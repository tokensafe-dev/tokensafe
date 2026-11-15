"""
TokenSafe Lockfile Parser & Reducer (v3.3.0).

Reduces bloated lockfiles (package-lock.json, Cargo.lock, poetry.lock, pnpm-lock.yaml)
from 10,000+ lines of integrity hashes and URLs down to a compact dependency bill:
- Direct packages and exact resolved versions
- Dependency count summary
- Reversible hash anchor pointing to the full raw file in TokenSafe stash.
"""

import json
import re
from typing import Tuple, Dict, Any, List, Optional
from src.stash import stash_block


class LockfileReducer:
    """Extracts compact dependency information from verbose lockfiles."""

    @classmethod
    def is_lockfile(cls, filename_or_path: str) -> bool:
        name = filename_or_path.lower().split("/")[-1].split("\\")[-1]
        return name in (
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "cargo.lock",
            "poetry.lock",
            "gemfile.lock",
            "composer.lock",
            "go.sum",
        )

    @classmethod
    def reduce(cls, content: str, filename: str = "package-lock.json") -> Tuple[str, bool]:
        """Reduces lockfile content to a clean summary. Returns (compacted_text, was_compacted)."""
        lines = content.splitlines()
        if len(lines) < 30:
            return content, False

        fname = filename.lower().split("/")[-1].split("\\")[-1]

        if fname == "package-lock.json":
            return cls._reduce_npm_lock(content)
        elif fname == "cargo.lock":
            return cls._reduce_cargo_lock(content)
        elif fname == "poetry.lock":
            return cls._reduce_poetry_lock(content)

        return cls._generic_reduce(content, fname)

    @classmethod
    def _reduce_npm_lock(cls, content: str) -> Tuple[str, bool]:
        try:
            data = json.loads(content)
        except Exception:
            return cls._generic_reduce(content, "package-lock.json")

        name = data.get("name") or "project"
        lock_version = data.get("lockfileVersion", 1)
        packages = data.get("packages", {})
        dependencies = data.get("dependencies", {})

        direct_deps = {}
        total_count = 0

        # Lockfile v2/v3
        if packages and isinstance(packages, dict):
            root_pkg = packages.get("") or {}
            root_deps = {**root_pkg.get("dependencies", {}), **root_pkg.get("devDependencies", {})}
            total_count = len(packages) - 1
            for dep_name, expected_ver in sorted(root_deps.items()):
                target_key = f"node_modules/{dep_name}"
                resolved_ver = packages.get(target_key, {}).get("version") or expected_ver
                direct_deps[dep_name] = resolved_ver
        elif dependencies and isinstance(dependencies, dict):
            # Lockfile v1
            total_count = len(dependencies)
            for dep_name, info in sorted(dependencies.items()):
                if isinstance(info, dict):
                    direct_deps[dep_name] = info.get("version", "*")

        lines = [
            f"# [TokenSafe Lockfile Summary: package-lock.json (v{lock_version})]",
            f"Project: {name} | Total installed packages: {total_count}",
            "",
            "## Direct Dependencies:"
        ]
        for dname, dver in direct_deps.items():
            lines.append(f"  - {dname}: {dver}")

        hash8, marker = stash_block(content, source="lockfile:npm")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _reduce_cargo_lock(cls, content: str) -> Tuple[str, bool]:
        # Cargo.lock format: [[package]] name = "..." version = "..."
        pkg_pattern = re.compile(r'\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')
        matches = pkg_pattern.findall(content)
        if not matches:
            return cls._generic_reduce(content, "Cargo.lock")

        lines = [
            f"# [TokenSafe Lockfile Summary: Cargo.lock]",
            f"Total resolved crates: {len(matches)}",
            "",
            "## Crates:"
        ]
        # Show top 40 unique packages
        seen = set()
        for name, ver in sorted(matches):
            if name not in seen:
                seen.add(name)
                lines.append(f"  - {name}: {ver}")

        hash8, marker = stash_block(content, source="lockfile:cargo")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _reduce_poetry_lock(cls, content: str) -> Tuple[str, bool]:
        pkg_pattern = re.compile(r'\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')
        matches = pkg_pattern.findall(content)
        if not matches:
            return cls._generic_reduce(content, "poetry.lock")

        lines = [
            f"# [TokenSafe Lockfile Summary: poetry.lock]",
            f"Total resolved Python packages: {len(matches)}",
            "",
            "## Packages:"
        ]
        for name, ver in sorted(matches):
            lines.append(f"  - {name}: {ver}")

        hash8, marker = stash_block(content, source="lockfile:poetry")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _generic_reduce(cls, content: str, fname: str) -> Tuple[str, bool]:
        lines = content.splitlines()
        head = lines[:20]
        tail = lines[-10:]
        omitted = len(lines) - 30
        hash8, marker = stash_block(content, source=f"lockfile:{fname}")
        out = (
            head +
            [f"\n... [{omitted} lines of lockfile metadata omitted by TokenSafe] ...\n"] +
            tail +
            ["", marker]
        )
        return "\n".join(out), True
