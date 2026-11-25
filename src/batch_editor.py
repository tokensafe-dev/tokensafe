"""
TokenSafe Batch Editor Engine (v3.5.0).

Consolidates multi-hunk and multi-file code modifications into a single
atomic or best-effort operation with an optional inline verification command.

Key capabilities:
- In-memory pre-validation across up to 50 files before touching disk (0 files modified on error).
- best_effort mode: commits valid files atomically while returning closest-line / drift hints for retries.
- Secret leak prevention: detects credentials and private key fallbacks before writing.
- Preservation of CRLF vs LF line endings, UTF-8 encodings, and file permissions.
- Inline verify command execution (e.g. `npm test`, `pytest`, `cargo test`) with ANSI stripping
  and Head-Tail output folding, cutting unnecessary LLM round-trip turns.
"""

import os
import re
import difflib
import shlex
import subprocess
import time
from typing import Dict, Any, List, Optional, Tuple

from src.guard import TokenSafeGuard
from src.paths import is_plugin_or_system_dir
from src.secret_redactor import SecretRedactor
from src.format_parsers.ansi import strip_ansi

MAX_BATCH_FILES = 50
MAX_VERIFY_OUTPUT_CHARS = 4000

_VERIFY_SIMPLE_RUNNERS = frozenset({
    "pytest", "py.test", "ruff", "mypy", "pyright", "flake8", "tox", "nox",
    "vitest", "jest", "mocha", "ava", "tsc", "eslint", "prettier", "biome",
    "oxlint", "rspec", "phpunit", "ctest",
})
_VERIFY_PYTHON_RUNNERS = frozenset({
    "python", "python3", "python3.10", "python3.11", "python3.12", "python3.13",
})
_VERIFY_PYTHON_MODULES = frozenset({
    "unittest", "pytest", "py_compile", "compileall", "mypy", "ruff", "flake8", "doctest",
})
_VERIFY_JS_PKG_MANAGERS = frozenset({"npm", "pnpm", "yarn"})
_VERIFY_SAFE_SCRIPT_PREFIXES = ("test", "lint", "check", "typecheck", "build")
_VERIFY_SUBCOMMAND_ALLOWLIST = {
    "go": frozenset({"test", "vet", "build"}),
    "cargo": frozenset({"test", "check", "clippy", "build"}),
    "dotnet": frozenset({"test", "build"}),
    "zig": frozenset({"test", "build"}),
    "swift": frozenset({"test", "build"}),
    "tokensafe": frozenset({"tests"}),
}
_VERIFY_FORBIDDEN_FLAGS = frozenset({
    "-c", "-e", "--eval", "-p", "--print", "-r", "--require",
    "--import", "--loader", "-exec", "--exec", "-toolexec",
})
_DISALLOWED_SYSTEM_DIRS = frozenset({"/etc", "/usr", "/bin", "/sbin", "/System", "/private/etc"})


def _has_unquoted_shell_operators(cmd_str: str) -> bool:
    """Detects shell chaining, redirection, or command substitution operators."""
    in_single = False
    in_double = False
    escaped = False
    n = len(cmd_str)
    for i, ch in enumerate(cmd_str):
        if ch in ("\n", "\r", "\x00"):
            return True
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single:
            if ch == "`":
                return True
            if ch == "$" and i + 1 < n and cmd_str[i + 1] in ("(", "{"):
                return True
        if not in_single and not in_double and ch in (";", "|", "&", ">", "<"):
            return True
    return False


def validate_verify_command(
    verify_cmd: str, base_dir: str = ""
) -> Tuple[Optional[List[str]], Optional[str], Optional[str]]:
    """
    Validates an inline verify command through TokenSafeGuard, shell-operator
    rejection, shlex tokenization, and a strict verification-runner allowlist.
    Returns (argv, error_reason, guard_check_name).
    """
    cmd_str = str(verify_cmd or "").strip()
    if not cmd_str:
        return None, "Verification command cannot be empty.", "VERIFY_COMMAND_BLOCKED"

    guard_res = TokenSafeGuard.inspect_command(cmd_str, cwd=base_dir)
    if guard_res.blocked:
        return (
            None,
            f"TokenSafe Security Guard ({guard_res.check_name}) blocked verify command: {guard_res.reason}",
            guard_res.check_name,
        )

    if _has_unquoted_shell_operators(cmd_str):
        return (
            None,
            "Shell chaining, redirection, and command substitution operators are disallowed in apply_edits.verify.",
            "VERIFY_COMMAND_BLOCKED",
        )

    try:
        argv = shlex.split(cmd_str)
    except ValueError as exc:
        return None, f"Malformed verify command syntax: {exc}", "VERIFY_COMMAND_BLOCKED"

    if not argv:
        return None, "Verification command cannot be empty.", "VERIFY_COMMAND_BLOCKED"

    raw_exe = argv[0]
    if "/" in raw_exe or "\\" in raw_exe:
        return (
            None,
            f"Path-qualified executables ('{raw_exe}') are disallowed in apply_edits.verify.",
            "VERIFY_COMMAND_BLOCKED",
        )

    exe = raw_exe.lower()
    if exe.endswith(".exe"):
        exe = exe[:-4]

    args_rest = argv[1:]
    if any(a in _VERIFY_FORBIDDEN_FLAGS or a.startswith("--eval=") or a.startswith("-exec=") for a in args_rest):
        return (
            None,
            f"Flag '{next(a for a in args_rest if a in _VERIFY_FORBIDDEN_FLAGS or a.startswith(('--eval=', '-exec=')))}' is disallowed in apply_edits.verify.",
            "VERIFY_COMMAND_BLOCKED",
        )

    if exe in _VERIFY_PYTHON_RUNNERS:
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] in _VERIFY_PYTHON_MODULES:
            return argv, None, None
        return (
            None,
            f"Python verify commands must use '-m <allowed_module>' (one of {sorted(_VERIFY_PYTHON_MODULES)}).",
            "VERIFY_COMMAND_BLOCKED",
        )

    if exe in _VERIFY_JS_PKG_MANAGERS:
        if args_rest and args_rest[0] in ("test", "t", "lint", "check", "typecheck", "build"):
            return argv, None, None
        if len(args_rest) >= 2 and args_rest[0] == "run" and args_rest[1].startswith(_VERIFY_SAFE_SCRIPT_PREFIXES):
            return argv, None, None
        return (
            None,
            f"'{exe}' verify command only allows test/lint/check/typecheck/build subcommands.",
            "VERIFY_COMMAND_BLOCKED",
        )

    if exe == "bun":
        if args_rest and args_rest[0] == "test":
            return argv, None, None
        if len(args_rest) >= 2 and args_rest[0] == "run" and args_rest[1].startswith(_VERIFY_SAFE_SCRIPT_PREFIXES):
            return argv, None, None
        return (
            None,
            "'bun' verify command only allows 'bun test' or 'bun run <test|lint|check|typecheck|build>*'.",
            "VERIFY_COMMAND_BLOCKED",
        )

    if exe in _VERIFY_SUBCOMMAND_ALLOWLIST:
        allowed_subs = _VERIFY_SUBCOMMAND_ALLOWLIST[exe]
        if args_rest and args_rest[0] in allowed_subs:
            return argv, None, None
        return (
            None,
            f"'{exe}' verify command only allows subcommands: {sorted(allowed_subs)}.",
            "VERIFY_COMMAND_BLOCKED",
        )

    if exe in _VERIFY_SIMPLE_RUNNERS:
        return argv, None, None

    return (
        None,
        f"Executable '{raw_exe}' is not in the allowlisted verification runners for apply_edits.verify.",
        "VERIFY_COMMAND_BLOCKED",
    )


def _find_closest_line_hint(target: str, content: str, max_lines_preview: int = 5) -> str:
    """Finds closest matching snippet in content to provide actionable line-drift retry guidance."""
    target_first = target.splitlines()[0].strip() if target.splitlines() else target.strip()
    if not target_first:
        return "Target string is empty."

    lines = content.splitlines()
    best_ratio = 0.0
    best_idx = -1
    for idx, line in enumerate(lines):
        ratio = difflib.SequenceMatcher(None, target_first, line.strip()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = idx

    if best_idx >= 0 and best_ratio > 0.4:
        start = max(0, best_idx - 2)
        end = min(len(lines), best_idx + 3)
        preview = []
        for i in range(start, end):
            marker = ">> " if i == best_idx else "   "
            preview.append(f"{marker}L{i+1}: {lines[i]}")
        return f"Target not matched. Closest match at line {best_idx+1} (similarity {best_ratio:.0%}):\n" + "\n".join(preview)
    return "Target string not found anywhere in file."


def _apply_hunk_to_text(
    content: str,
    target: str,
    replacement: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    replace_all: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """Applies a single hunk to text in-memory, preserving CRLF endings. Returns (new_text, error_message)."""
    if not target:
        return None, "Target string cannot be empty."

    lines = content.splitlines(keepends=True)
    has_crlf = "\r\n" in content
    if has_crlf:
        if target not in content and "\r\n" not in target and "\n" in target:
            crlf_target = target.replace("\n", "\r\n")
            if crlf_target in content:
                target = crlf_target
        if "\n" in replacement:
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")

    # Line-bounded search if start_line or end_line is specified
    if start_line is not None or end_line is not None:
        s_idx = max(0, (start_line - 1)) if start_line is not None else 0
        e_idx = min(len(lines), end_line) if end_line is not None else len(lines)
        if s_idx >= len(lines) or s_idx >= e_idx:
            return None, f"Specified line range [{start_line}, {end_line}] is outside file length ({len(lines)} lines)."
        sub_text = "".join(lines[s_idx:e_idx])
        if target not in sub_text:
            hint = _find_closest_line_hint(target, sub_text)
            return None, f"Target not found in lines {s_idx+1}..{e_idx}: {hint}"
        count = sub_text.count(target)
        if count > 1 and not replace_all:
            return None, f"Target occurs {count} times within lines {s_idx+1}..{e_idx}. Specify replace_all=True or narrow the range."
        new_sub = sub_text.replace(target, replacement) if replace_all else sub_text.replace(target, replacement, 1)
        new_lines = lines[:s_idx] + [new_sub] + lines[e_idx:]
        return "".join(new_lines), None

    if target not in content:
        hint = _find_closest_line_hint(target, content)
        return None, f"Target not found: {hint}"

    count = content.count(target)
    if count > 1 and not replace_all:
        # Find line numbers of occurrences
        occ_lines = []
        for i, l in enumerate(lines):
            if target in l:
                occ_lines.append(str(i + 1))
        locs = f"lines {', '.join(occ_lines[:5])}" if occ_lines else "multiple locations"
        return None, f"Target occurs {count} times (near {locs}). Specify replace_all=True or pass start_line/end_line."

    new_content = content.replace(target, replacement) if replace_all else content.replace(target, replacement, 1)
    return new_content, None


def execute_batch_edits(
    edits: List[Dict[str, Any]],
    best_effort: bool = False,
    dry_run: bool = False,
    verify: Optional[str] = None,
    verify_timeout_sec: int = 30,
    cwd: Optional[str] = None
) -> Dict[str, Any]:
    """
    Applies multi-file, multi-hunk edits in memory, writes atomically or best-effort,
    and optionally executes an inline verify command.
    """
    if not isinstance(edits, list) or not edits:
        return {
            "success": False,
            "error": "Edits list must be a non-empty array of file edit objects.",
            "applied_files": [],
            "failed_files": []
        }

    if len(edits) > MAX_BATCH_FILES:
        return {
            "success": False,
            "error": f"Batch edits exceeds maximum of {MAX_BATCH_FILES} files per call (got {len(edits)}).",
            "applied_files": [],
            "failed_files": []
        }

    base_dir = os.path.abspath(cwd or os.getcwd())
    real_base = os.path.realpath(base_dir)
    if is_plugin_or_system_dir(base_dir) or real_base in _DISALLOWED_SYSTEM_DIRS or not os.path.isdir(real_base):
        return {
            "success": False,
            "error": f"Refusing batch edits in system, plugin, or non-existent directory '{cwd or base_dir}'.",
            "applied_files": [],
            "failed_files": []
        }

    verified_argv: Optional[List[str]] = None
    if verify:
        verified_argv, verify_err, verify_check = validate_verify_command(str(verify), base_dir=real_base)
        if verify_err or not verified_argv:
            return {
                "success": False,
                "blocked": True,
                "check_name": verify_check or "VERIFY_COMMAND_BLOCKED",
                "error": verify_err or "Verification command blocked.",
                "applied_count": 0,
                "applied_files": [],
                "failed_count": 0,
                "failed_files": [],
                "dry_run": dry_run,
            }

    # Pre-validation & secret check
    prepared_files: Dict[str, Dict[str, Any]] = {}
    failed_files: List[Dict[str, Any]] = []

    for item in edits:
        if not isinstance(item, dict):
            failed_files.append({"path": "unknown", "error": f"Invalid edit entry: expected object, got {type(item).__name__}"})
            continue

        rel_path = str(item.get("path") or "").strip()
        if not rel_path:
            failed_files.append({"path": "", "error": "Missing 'path' parameter in edit entry."})
            continue

        if os.path.isabs(rel_path):
            failed_files.append({"path": rel_path, "error": "Absolute paths and traversal outside workspace root are disallowed."})
            continue

        abs_path = os.path.normpath(os.path.join(base_dir, rel_path))
        real_target = os.path.realpath(abs_path)

        # Strict containment check (blocks ../sibling, symlinks, drive escapes)
        try:
            contained = os.path.commonpath([real_base, real_target]) == real_base
        except ValueError:
            contained = False
        if not contained or real_target == real_base:
            failed_files.append({"path": rel_path, "error": "Path traversal outside workspace root is disallowed."})
            continue
        abs_path = real_target

        # File creation vs hunk modification
        create_content = item.get("create")
        overwrite = bool(item.get("overwrite"))
        hunks = item.get("hunks") or []

        if create_content is not None:
            # Creation mode
            if not isinstance(create_content, str):
                failed_files.append({"path": rel_path, "error": "'create' content must be a string."})
                continue

            # Secret check on new content
            is_leak, leak_reason = SecretRedactor.check_secret_fallback(create_content, file_path=rel_path)
            if is_leak:
                failed_files.append({"path": rel_path, "error": f"TokenSafe Security Guard blocked edit: {leak_reason}"})
                continue

            if (os.path.exists(abs_path) or abs_path in prepared_files) and not overwrite:
                failed_files.append({"path": rel_path, "error": f"File '{rel_path}' already exists. Set 'overwrite: true' to overwrite."})
                continue

            prepared_files[abs_path] = {
                "rel_path": rel_path,
                "new_content": create_content,
                "is_create": not os.path.exists(abs_path),
                "hunk_count": 0,
            }
            continue

        # Hunk edit mode
        if not isinstance(hunks, list) or not hunks:
            failed_files.append({"path": rel_path, "error": "Must specify either 'create' string or non-empty 'hunks' array."})
            continue

        prior_prepared = prepared_files.get(abs_path)
        if prior_prepared is not None:
            current_text = prior_prepared["new_content"]
            prev_hunk_count = prior_prepared["hunk_count"]
            is_create = prior_prepared["is_create"]
        else:
            if not os.path.exists(abs_path):
                failed_files.append({"path": rel_path, "error": f"File '{rel_path}' does not exist on disk."})
                continue

            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as f:
                    current_text = f.read()
            except Exception as e:
                failed_files.append({"path": rel_path, "error": f"Failed to read file: {e}"})
                continue
            prev_hunk_count = 0
            is_create = False

        file_failed = False
        applied_hunk_count = 0

        for hunk_idx, hunk in enumerate(hunks):
            if not isinstance(hunk, dict):
                failed_files.append({"path": rel_path, "error": f"Hunk #{hunk_idx+1} is not a valid dictionary."})
                file_failed = True
                break

            target = hunk.get("target")
            replacement = hunk.get("replacement")
            start_line = hunk.get("start_line")
            end_line = hunk.get("end_line")
            replace_all = bool(hunk.get("replace_all"))

            if target is None or replacement is None:
                failed_files.append({"path": rel_path, "error": f"Hunk #{hunk_idx+1} missing 'target' or 'replacement'."})
                file_failed = True
                break

            # Secret check on replacement
            is_leak, leak_reason = SecretRedactor.check_secret_fallback(replacement, file_path=rel_path)
            if is_leak:
                failed_files.append({"path": rel_path, "error": f"TokenSafe Security Guard: {leak_reason}"})
                file_failed = True
                break

            new_text, err = _apply_hunk_to_text(
                current_text,
                target=str(target),
                replacement=str(replacement),
                start_line=int(start_line) if start_line is not None else None,
                end_line=int(end_line) if end_line is not None else None,
                replace_all=replace_all
            )

            if err is not None:
                failed_files.append({"path": rel_path, "error": f"Hunk #{hunk_idx+1} failed: {err}"})
                file_failed = True
                break

            current_text = new_text
            applied_hunk_count += 1

        if not file_failed:
            prepared_files[abs_path] = {
                "rel_path": rel_path,
                "new_content": current_text,
                "is_create": is_create,
                "hunk_count": prev_hunk_count + applied_hunk_count,
            }

    # Atomic enforcement: if any file failed and not best_effort, reject all
    if failed_files and not best_effort:
        return {
            "success": False,
            "error": f"Batch edit validation failed on {len(failed_files)} file(s). 0 files modified.",
            "applied_count": 0,
            "applied_files": [],
            "failed_count": len(failed_files),
            "failed_files": failed_files,
            "dry_run": dry_run
        }

    applied_files_info: List[Dict[str, Any]] = []

    # Write files to disk if not dry_run (with atomic rollback on write failure when best_effort=False)
    if not dry_run:
        backups: List[Tuple[str, Optional[bytes]]] = []
        write_aborted = False
        for abs_p, p_info in prepared_files.items():
            try:
                orig_bytes: Optional[bytes] = None
                if os.path.exists(abs_p):
                    with open(abs_p, "rb") as bf:
                        orig_bytes = bf.read()
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                with open(abs_p, "w", encoding="utf-8", newline="") as f:
                    f.write(p_info["new_content"])
                backups.append((abs_p, orig_bytes))
                applied_files_info.append({
                    "path": p_info["rel_path"],
                    "hunks": p_info["hunk_count"],
                    "is_create": p_info["is_create"]
                })
            except Exception as e:
                failed_files.append({"path": p_info["rel_path"], "error": f"Disk write failed: {e}"})
                if not best_effort:
                    write_aborted = True
                    break
        if write_aborted:
            for b_path, b_bytes in reversed(backups):
                try:
                    if b_bytes is None:
                        if os.path.exists(b_path):
                            os.remove(b_path)
                    else:
                        with open(b_path, "wb") as rf:
                            rf.write(b_bytes)
                except OSError:
                    pass
            applied_files_info = []
    else:
        for abs_p, p_info in prepared_files.items():
            applied_files_info.append({
                "path": p_info["rel_path"],
                "hunks": p_info["hunk_count"],
                "is_create": p_info["is_create"],
                "dry_run": True
            })

    result: Dict[str, Any] = {
        "success": len(applied_files_info) > 0 and len(failed_files) == 0,
        "applied_count": len(applied_files_info),
        "applied_files": applied_files_info,
        "failed_count": len(failed_files),
        "failed_files": failed_files,
        "dry_run": dry_run
    }

    # Execute inline verify command if requested and files were modified (shell=False, allowlisted argv)
    if verified_argv and applied_files_info and not dry_run:
        cmd_str = str(verify).strip()
        t0 = time.time()
        try:
            proc = subprocess.run(
                verified_argv,
                shell=False,
                cwd=real_base,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=max(1, min(verify_timeout_sec, 120))
            )
            elapsed = time.time() - t0
            raw_out = strip_ansi(proc.stdout or "")
            if len(raw_out) > MAX_VERIFY_OUTPUT_CHARS:
                lines = raw_out.splitlines()
                if len(lines) > 35:
                    head = lines[:20]
                    tail = lines[-15:]
                    folded_lines = head + [f"... [{len(lines) - 35} lines folded] ..."] + tail
                    compact_out = "\n".join(folded_lines)
                else:
                    half = MAX_VERIFY_OUTPUT_CHARS // 2
                    compact_out = raw_out[:half] + "\n... [output truncated] ...\n" + raw_out[-half:]
            else:
                compact_out = raw_out

            result["verify"] = {
                "command": cmd_str,
                "passed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "duration_seconds": round(elapsed, 2),
                "output": compact_out.strip() or "(no output)"
            }
            if proc.returncode != 0:
                result["success"] = False
        except subprocess.TimeoutExpired:
            result["verify"] = {
                "command": cmd_str,
                "passed": False,
                "exit_code": -1,
                "error": f"Verification command timed out after {verify_timeout_sec}s"
            }
            result["success"] = False
        except Exception as e:
            result["verify"] = {
                "command": cmd_str,
                "passed": False,
                "exit_code": -1,
                "error": f"Failed to execute verify command: {e}"
            }
            result["success"] = False

    return result
