"""
TokenSafe Universal Build Output Compactor (v4.1.3).

Specialized compactor for:
1. `vite build` / Rollup:
   - Strips carriage return (\\r) progress spam (`transforming (1)...`, `rendering chunks...`, `computing gzip size...`).
   - Folds multi-line `dist/assets/...` tables into clean entrypoint summaries.
   - Preserves custom pre/post-build hooks (prerendering, manifests, sitemaps).
   - Preserves 100% of Rollup/Vite/TypeScript errors on build failures.
2. `next build`:
   - Strips `Generating static pages (x/y)` progress noise.
   - Folds large `Route (app|pages)` tables and shared chunk dumps into stash.
   - Preserves Next.js route count, entry routes, and shared JS summary.
3. Webpack / Create React App:
   - Strips file size table bloat, keeping main bundle size and deploy readiness.
4. Cargo build:
   - Folds dozens of `Compiling crate vX.Y.Z` lines into a compact crate count summary.
5. Generic build:
   - Bounded head + tail folding with reversible stash anchors.
"""

import os
import re
from typing import Dict, Any, List, Optional, Tuple
from src.stash import stash_block
from src.format_parsers.ansi import strip_ansi

# Patterns for build detection
_VITE_START_RE = re.compile(r"\bvite\s+v?[\d.]+\s+building\s+for\s+production\b", re.IGNORECASE)
_VITE_BUILT_RE = re.compile(r"✓\s*built\s+in\s+([\d.]+[mµn]?s)", re.IGNORECASE)
_VITE_MODULES_RE = re.compile(r"✓\s*(\d+)\s+modules\s+transformed", re.IGNORECASE)
_VITE_ASSET_LINE_RE = re.compile(r"^(dist/[\w./-]+)\s+([\d.]+\s+[kM]?B)(?:\s+│\s+gzip:\s+([\d.]+\s+[kM]?B))?", re.MULTILINE)

_NEXT_START_RE = re.compile(r"▲\s*Next\.js\s+v?[\d.]+|Creating an optimized production build\s*\.\.\.", re.IGNORECASE)
_NEXT_COMPILED_RE = re.compile(r"✓\s*Compiled\s+successfully", re.IGNORECASE)
_NEXT_ROUTE_ROW_RE = re.compile(r"^[┌├└│]\s*[○●λ]\s*(\S+)\s+([\d.]+\s+[kM]?B)\s+([\d.]+\s+[kM]?B)", re.MULTILINE)

_WEBPACK_COMPILED_RE = re.compile(r"Compiled\s+successfully\.", re.IGNORECASE)
_WEBPACK_FILE_SIZES_RE = re.compile(r"File\s+sizes\s+after\s+gzip:", re.IGNORECASE)

_CARGO_FINISHED_RE = re.compile(r"Finished\s+(release|dev|bench)\s+\[(.*?)\]\s+target\(s\)\s+in\s+([\d.]+[a-zA-Z]*)", re.IGNORECASE)
_CARGO_COMPILING_LINE_RE = re.compile(r"^\s*Compiling\s+(\S+)\s+v(\S+)", re.MULTILINE)

_FAILURE_SIGNALS_RE = re.compile(
    r"\b(error\s+TS\d+|SyntaxError|Failed\s+to\s+compile|error\s+during\s+build|"
    r"Rollup\s+failed\s+to\s+resolve|Could\s+not\s+resolve|error\[E\d+\]|"
    r"could\s+not\s+compile|UnhandledSchemeError|ModuleNotFoundException)\b",
    re.IGNORECASE
)


class BuildOutputParser:
    """Universal Build Output Compactor for Vite, Rollup, Next.js, Webpack, CRA, and Cargo."""

    @classmethod
    def clean_progress_noise(cls, text: str) -> str:
        """Removes interactive TTY carriage returns and progress counter artifacts."""
        if not text:
            return ""

        # Handle \\r line overwrites: take the last non-empty segment of \\r splits
        lines: List[str] = []
        for raw_line in text.splitlines():
            if "\r" in raw_line:
                segments = [s.strip() for s in raw_line.split("\r") if s.strip()]
                if segments:
                    lines.append(segments[-1])
            else:
                lines.append(raw_line)

        cleaned = "\n".join(lines)

        # Strip concatenated Vite progress artifacts when \\r was flattened
        cleaned = re.sub(r"(?:transforming\s*\(\d+\)[^\n✓]*)+", "", cleaned)
        cleaned = re.sub(r"(?:rendering\s*chunks\s*\(\d+\)\.{3})+", "", cleaned)
        cleaned = re.sub(r"(?:computing\s*gzip\s*size\s*\(\d+\)\.{3})+", "", cleaned)

        # Strip Next.js page generation progress
        cleaned = re.sub(r"(?:Generating\s*static\s*pages\s*\(\d+/\d+\)\s*\.{0,3}\s*)+", "", cleaned)

        return cleaned

    @classmethod
    def detect_runner(cls, raw: str, cmd: Optional[str] = None) -> Optional[str]:
        """Classifies the build tool from command string or terminal output signatures."""
        cmd_lower = (cmd or "").lower()
        if "vite" in cmd_lower:
            return "vite"
        if "next" in cmd_lower and "build" in cmd_lower:
            return "next"
        if "cargo" in cmd_lower and "build" in cmd_lower:
            return "cargo"

        # Check content signatures
        if _VITE_START_RE.search(raw) or _VITE_BUILT_RE.search(raw) or ("modules transformed" in raw and "dist/" in raw):
            return "vite"
        if _NEXT_START_RE.search(raw) or "Route (app)" in raw or "Route (pages)" in raw:
            return "next"
        if _WEBPACK_COMPILED_RE.search(raw) or _WEBPACK_FILE_SIZES_RE.search(raw):
            return "webpack"
        if _CARGO_FINISHED_RE.search(raw) or ("Compiling " in raw and "Finished " in raw):
            return "cargo"
        if "[bun] Built in" in raw:
            return "bun"

        if cmd_lower and any(b in cmd_lower for b in ("run build", "npm build", "yarn build", "pnpm build", "bun build")):
            return "generic_build"

        return None

    @classmethod
    def compact(cls, raw: str, exit_code: int = 0, cmd: Optional[str] = None) -> Tuple[str, bool]:
        """
        Compacts build tool output.
        - If exit_code != 0 or failure detected: strips progress noise but keeps 100% of compiler errors.
        - If exit_code == 0: collapses asset/route tables into clean structured summaries with stash anchors.
        Returns (compacted_text, was_build_tool).
        """
        if not raw or not raw.strip():
            return raw, False

        clean_text = strip_ansi(raw)
        runner = cls.detect_runner(clean_text, cmd)
        if not runner:
            return raw, False

        cleaned = cls.clean_progress_noise(clean_text)
        is_failure = (exit_code != 0) or bool(_FAILURE_SIGNALS_RE.search(cleaned))

        if is_failure:
            return cls._compact_failure(cleaned, runner, raw)

        if runner == "vite":
            return cls._compact_vite_success(cleaned, raw)
        elif runner == "next":
            return cls._compact_next_success(cleaned, raw)
        elif runner == "webpack":
            return cls._compact_webpack_success(cleaned, raw)
        elif runner == "cargo":
            return cls._compact_cargo_success(cleaned, raw)
        else:
            return cls._compact_generic_success(cleaned, raw)

    @classmethod
    def _compact_failure(cls, cleaned: str, runner: str, raw_original: str) -> Tuple[str, bool]:
        """Failure mode: preserves all error frames and tracebacks; removes only progress noise."""
        lines = [ln for ln in cleaned.splitlines() if ln.strip()]
        # Filter out purely informational header lines if errors follow
        error_lines: List[str] = []
        for ln in lines:
            if "transforming (" in ln or "rendering chunks" in ln or "computing gzip size" in ln:
                continue
            error_lines.append(ln)

        result = "\n".join(error_lines).strip()
        return result or cleaned.strip(), True

    @classmethod
    def _compact_vite_success(cls, cleaned: str, raw_original: str) -> Tuple[str, bool]:
        """Compacts Vite / Rollup successful build output."""
        time_match = _VITE_BUILT_RE.search(cleaned)
        dur = time_match.group(1) if time_match else ""

        modules_match = _VITE_MODULES_RE.search(cleaned)
        modules = modules_match.group(1) if modules_match else ""

        assets = _VITE_ASSET_LINE_RE.findall(cleaned)

        # Milestone lines (manifest, prerender, custom script logs)
        milestones: List[str] = []
        for ln in cleaned.splitlines():
            s = ln.strip()
            if s.startswith("✓") and not s.startswith("✓ built in") and not "modules transformed" in s:
                milestones.append(s)
            elif "Prerendered" in s or "sitemap.xml" in s:
                if s not in milestones:
                    milestones.append(s)

        out_lines: List[str] = []
        summary_parts = []
        if dur:
            summary_parts.append(f"in {dur}")
        if modules:
            summary_parts.append(f"{int(modules):,} modules transformed")
        if assets:
            summary_parts.append(f"{len(assets)} assets generated in dist/")

        meta_desc = f" ({', '.join(summary_parts)})" if summary_parts else ""
        out_lines.append(f"✓ vite build succeeded{meta_desc}")

        # Keep primary HTML entry point and largest JS chunk if available
        if assets:
            html_assets = [a for a in assets if a[0].endswith(".html")]
            js_assets = [a for a in assets if a[0].endswith(".js")]
            other_assets = [a for a in assets if not a[0].endswith(".html") and not a[0].endswith(".js")]

            for path, size, gz in html_assets[:2]:
                gz_str = f" (gzip: {gz})" if gz else ""
                out_lines.append(f"  • {path}: {size}{gz_str}")

            if js_assets:
                # Top largest JS bundle
                top_js = js_assets[-1]
                gz_str = f" (gzip: {top_js[2]})" if top_js[2] else ""
                out_lines.append(f"  • {top_js[0]}: {top_js[1]}{gz_str}")

        # Add milestones
        for m in milestones:
            out_lines.append(m)

        # Stash full raw output if assets > 4
        if len(assets) > 4 or len(cleaned.splitlines()) > 20:
            _, marker = stash_block(raw_original, source="vite_build")
            out_lines.append(marker)

        return "\n".join(out_lines), True

    @classmethod
    def _compact_next_success(cls, cleaned: str, raw_original: str) -> Tuple[str, bool]:
        """Compacts Next.js successful build output."""
        routes = _NEXT_ROUTE_ROW_RE.findall(cleaned)

        static_pages = 0
        pages_match = re.search(r"Generating static pages \((\d+)/(\d+)\)", raw_original)
        if pages_match:
            static_pages = int(pages_match.group(2))

        shared_js = ""
        shared_match = re.search(r"\+\s*First\s+Load\s+JS\s+shared\s+by\s+all\s+([\d.]+\s+[kM]?B)", cleaned)
        if shared_match:
            shared_js = shared_match.group(1)

        out_lines: List[str] = []
        route_desc = f" ({len(routes)} routes" if routes else " ("
        if static_pages:
            route_desc += f", {static_pages} static pages" if routes else f"{static_pages} static pages"
        route_desc += ")" if "(" in route_desc and route_desc != " (" else ""

        out_lines.append(f"✓ Next.js build succeeded{route_desc}")

        if routes:
            for path, size, first_load in routes[:3]:
                out_lines.append(f"  • {path} ({size} | load: {first_load})")

        if shared_js:
            out_lines.append(f"  • Shared JS bundle: {shared_js}")

        if len(routes) > 3 or len(cleaned.splitlines()) > 20:
            _, marker = stash_block(raw_original, source="next_build")
            out_lines.append(marker)

        return "\n".join(out_lines), True

    @classmethod
    def _compact_webpack_success(cls, cleaned: str, raw_original: str) -> Tuple[str, bool]:
        """Compacts Webpack / Create React App successful build output."""
        out_lines = ["✓ Webpack build succeeded (Build folder is ready to be deployed)"]

        # Extract file sizes if present
        sizes_match = re.search(r"File sizes after gzip:\s*\n((?:\s*[\d.]+\s+[kM]?B\s+\S+\n?)+)", cleaned)
        if sizes_match:
            size_lines = [ln.strip() for ln in sizes_match.group(1).splitlines() if ln.strip()]
            for sln in size_lines[:2]:
                out_lines.append(f"  • {sln}")
            if len(size_lines) > 2:
                out_lines.append(f"  ... (+{len(size_lines) - 2} more asset chunks folded)")

        _, marker = stash_block(raw_original, source="webpack_build")
        out_lines.append(marker)
        return "\n".join(out_lines), True

    @classmethod
    def _compact_cargo_success(cls, cleaned: str, raw_original: str) -> Tuple[str, bool]:
        """Compacts Cargo build successful output."""
        compiling_matches = _CARGO_COMPILING_LINE_RE.findall(cleaned)
        finished_match = _CARGO_FINISHED_RE.search(cleaned)

        profile = finished_match.group(1) if finished_match else "target"
        flags = finished_match.group(2) if finished_match else ""
        dur = finished_match.group(3) if finished_match else ""

        flags_str = f" [{flags}]" if flags else ""
        dur_str = f" in {dur}" if dur else ""
        crates_str = f" (compiled {len(compiling_matches)} crates)" if compiling_matches else ""

        out_lines = [f"✓ Finished {profile}{flags_str} target(s){dur_str}{crates_str}"]

        if len(compiling_matches) > 4:
            _, marker = stash_block(raw_original, source="cargo_build")
            out_lines.append(marker)

        return "\n".join(out_lines), True

    @classmethod
    def _compact_generic_success(cls, cleaned: str, raw_original: str) -> Tuple[str, bool]:
        """Generic fallback for build commands with long asset/progress outputs."""
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if len(lines) <= 12:
            return cleaned.strip(), True

        out_lines = [lines[0]]
        # Find any milestone lines with checkmarks
        for ln in lines[1:-1]:
            if ln.startswith("✓") or "succeeded" in ln.lower() or "completed" in ln.lower():
                out_lines.append(ln)

        out_lines.append(lines[-1])
        _, marker = stash_block(raw_original, source="generic_build")
        out_lines.append(marker)

        return "\n".join(out_lines), True
