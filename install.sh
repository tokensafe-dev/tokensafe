#!/usr/bin/env bash
# Installation script for TokenSafe Plugin
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.gemini/config/plugins/tokensafe"
VENV_DIR="${HOME}/.tokensafe/venv"

echo "⚡ Installing TokenSafe Plugin..."

mkdir -p "${HOME}/.gemini/config/plugins"

# Migrate away from the legacy plugin directory name: Antigravity keys plugin
# enable-state by folder name, so a stale legacy symlink would register the
# plugin twice and run every hook twice per turn. Remove only symlinks.
LEGACY_DIR="${HOME}/.gemini/config/plugins/agy-token-optimizer"
if [ -L "${LEGACY_DIR}" ]; then
    echo "  Removing legacy symlink at ${LEGACY_DIR}..."
    rm -f "${LEGACY_DIR}"
fi

if [ -L "${TARGET_DIR}" ] || [ -d "${TARGET_DIR}" ]; then
    echo "  Removing existing installation at ${TARGET_DIR}..."
    rm -rf "${TARGET_DIR}"
fi

echo "  Creating symlink: ${TARGET_DIR} -> ${SCRIPT_DIR}"
ln -s "${SCRIPT_DIR}" "${TARGET_DIR}"

# >>> bootstrap-python >>>
# Runtime resolution shared by tokensafe/install.sh (Gemini/Antigravity plugin)
# and tokensafe.dev/install.sh. Both copies must stay byte-identical:
# tests/test_install_scripts.py fails on drift. Two Windows traps it covers:
#
#   1. `command -v python3` is not proof of Python. Windows ships a Microsoft
#      Store App Execution Alias named python3.exe / python.exe that prints
#      "Python was not found" and exits 9009. Trusting it turned a machine with
#      no interpreter into a bogus "cryptography failed to install" verdict.
#   2. A Windows venv has Scripts/python.exe, not bin/python3, so the old venv
#      fallback could never install the dependency on Windows.

ts_is_windows() {
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*|Windows_NT) return 0 ;;
    esac
    return 1
}

# Prints the interpreter's own path when it runs CPython >= 3.10; fails otherwise.
# The version check runs before the path is printed so a rejected candidate never
# leaks output into a caller's command substitution.
ts_probe_python() {
    "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || return 1
    "$@" -c 'import sys; print(sys.executable)' 2>/dev/null
}

ts_has_cryptography() {
    "$1" -c 'import cryptography' >/dev/null 2>&1
}

ts_venv_python() {
    if [ -f "$1/Scripts/python.exe" ]; then
        printf '%s\n' "$1/Scripts/python.exe"
    elif [ -f "$1/bin/python3" ]; then
        printf '%s\n' "$1/bin/python3"
    fi
}

# Sets TS_PYTHON: honours TOKENSAFE_PYTHON, reuses a CPython the npm launcher
# already provisioned, then falls back to python3 / python / the Windows `py`
# launcher. Returns non-zero when no usable interpreter exists at all.
ts_resolve_python() {
    TS_PYTHON=""
    local candidate resolved

    if [ -n "${TOKENSAFE_PYTHON:-}" ]; then
        if ! resolved=$(ts_probe_python "${TOKENSAFE_PYTHON}"); then
            echo "✖ TOKENSAFE_PYTHON=${TOKENSAFE_PYTHON} does not run CPython 3.10+."
            return 1
        fi
        TS_PYTHON="${resolved}"
        return 0
    fi

    for candidate in "${HOME}"/.tokensafe/runtime/*/python/bin/python3 \
                     "${HOME}"/.tokensafe/runtime/*/python/python.exe; do
        [ -f "${candidate}" ] || continue
        if resolved=$(ts_probe_python "${candidate}"); then
            TS_PYTHON="${resolved}"
            return 0
        fi
    done

    for candidate in python3 python; do
        resolved=$(command -v "${candidate}" 2>/dev/null) || continue
        # Running the Store alias would open the Microsoft Store, not Python.
        case "${resolved}" in
            *[Ww]indows[Aa]pps*) continue ;;
        esac
        if resolved=$(ts_probe_python "${candidate}"); then
            TS_PYTHON="${resolved}"
            return 0
        fi
    done

    if command -v py >/dev/null 2>&1; then
        if resolved=$(ts_probe_python py -3); then
            TS_PYTHON="${resolved}"
            return 0
        fi
    fi

    return 1
}

# Installs the one required dependency and points TS_PYTHON at the interpreter
# that ended up owning it (a private venv when the base interpreter is locked
# down, e.g. PEP 668 or a read-only Store install).
ts_bootstrap_dependencies() {
    local python="$1" venv_dir="$2" output venv_py

    TS_PYTHON="${python}"
    if ts_has_cryptography "${python}"; then
        return 0
    fi

    echo "  Installing required dependency: cryptography>=42.0..."
    if output=$("${python}" -m pip install --user --quiet --disable-pip-version-check "cryptography>=42.0" 2>&1) \
        && ts_has_cryptography "${python}"; then
        return 0
    fi
    if output=$("${python}" -m pip install --quiet --break-system-packages "cryptography>=42.0" 2>&1) \
        && ts_has_cryptography "${python}"; then
        return 0
    fi
    if "${python}" -m venv "${venv_dir}" >/dev/null 2>&1; then
        venv_py=$(ts_venv_python "${venv_dir}")
        if [ -n "${venv_py}" ] \
            && output=$("${venv_py}" -m pip install --quiet --disable-pip-version-check "cryptography>=42.0" 2>&1) \
            && ts_has_cryptography "${venv_py}"; then
            TS_PYTHON="${venv_py}"
            return 0
        fi
    fi

    echo "✖ Could not install 'cryptography>=42.0' with ${python}."
    if ! "${python}" -m pip --version >/dev/null 2>&1; then
        echo "  That interpreter has no pip. Fix: \"${python}\" -m ensurepip --upgrade"
    fi
    if [ -n "${output}" ]; then
        printf '%s\n' "${output}" | tail -n 3 | sed 's/^/     /'
    fi
    echo "  Then re-run this installer, or install the dependency yourself:"
    echo "     \"${python}\" -m pip install 'cryptography>=42.0'"
    return 1
}

ts_print_no_python_guidance() {
    echo "✖ No usable CPython 3.10+ was found."
    if ts_is_windows; then
        echo "  On Windows 'python3' can be a Microsoft Store placeholder, not Python."
        echo "  Install Python once:      winget install Python.Python.3.12"
        echo "  Or use the npm package:   npm i -g tokensafe   (ships a pinned CPython)"
    else
        echo "  macOS:         brew install python3"
        echo "  Debian/Ubuntu: sudo apt-get install python3 python3-venv"
        echo "  Fedora:        sudo dnf install python3"
    fi
    echo "  Any platform:  https://www.python.org/downloads/"
    echo "  Point at one:  TOKENSAFE_PYTHON=/path/to/python3 (prefix the command)"
}
# <<< bootstrap-python <<<

# >>> windows-cli-shim >>>
# cmd.exe and PowerShell cannot run a POSIX symlink or a shebang script, and
# MSYS `ln -s` copies files instead of linking them, so `curl | bash` used to
# leave a Windows machine with a working engine and no `tokensafe` command at
# all: every "run tokensafe login" hint then failed with "not recognized".
# This block writes native .cmd shims and puts their directory on the user PATH,
# preserving REG_EXPAND_SZ entries and broadcasting WM_SETTINGCHANGE so terminals
# opened after the install see the new PATH.

ts_install_windows_cmd_shim() {
    local interpreter="$1" cli_script="$2" bin_dir="$3"
    local win_interpreter="${interpreter}" win_cli="${cli_script}" win_bin="${bin_dir}"

    # Only POSIX-style paths need translating; `node` (a bare command name) must
    # stay as it is so the shim resolves it from PATH.
    if command -v cygpath >/dev/null 2>&1; then
        case "${interpreter}" in /*) win_interpreter=$(cygpath -w "${interpreter}") ;; esac
        case "${cli_script}" in /*) win_cli=$(cygpath -w "${cli_script}") ;; esac
        case "${bin_dir}" in /*) win_bin=$(cygpath -w "${bin_dir}") ;; esac
    fi

    mkdir -p "${bin_dir}"
    # A bare command name (the Node fallback) stays unquoted, an absolute
    # Windows path is quoted so spaces in a profile name survive.
    local interpreter_token="${win_interpreter}"
    case "${win_interpreter}" in
        *:*) interpreter_token="\"${win_interpreter}\"" ;;
    esac
    # CRLF: cmd.exe parses batch files line by line and LF-only scripts break on
    # the first label continuation.
    printf '@echo off\r\n%s "%s" %%*\r\n' "${interpreter_token}" "${win_cli}" > "${bin_dir}/tokensafe.cmd"
    cp "${bin_dir}/tokensafe.cmd" "${bin_dir}/agy-tokens.cmd"

    if ts_windows_path_add "${win_bin}"; then
        echo "  Created ${bin_dir}/tokensafe.cmd and added ${win_bin} to the PATH for new terminals."
    else
        echo "  Created ${bin_dir}/tokensafe.cmd — add ${win_bin} to PATH to run it as 'tokensafe'."
    fi
}

# Adds a directory to the user PATH. Returns non-zero when PowerShell is
# unavailable, so the caller can print the manual step instead.
ts_windows_path_add() {
    local win_dir="$1"
    command -v powershell >/dev/null 2>&1 || return 1

    TS_PATH_ADD="${win_dir}" powershell -NoProfile -NonInteractive -Command - >/dev/null 2>&1 <<'POWERSHELL'
$dir = $env:TS_PATH_ADD
if (-not $dir) { exit 1 }
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
if (-not $key) { exit 1 }
$current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
if ($current -notlike "*$dir*") {
    $trimmed = $current.TrimEnd(';')
    $updated = if ($trimmed) { $trimmed + ';' + $dir } else { $dir }
    $key.SetValue('Path', $updated, [Microsoft.Win32.RegistryValueKind]::ExpandString)
}
$key.Close()
$source = @"
using System;
using System.Runtime.InteropServices;
public static class TokenSafeNative {
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
}
"@
if (Add-Type -TypeDefinition $source -ErrorAction SilentlyContinue) {
    $result = [UIntPtr]::Zero
    [void][TokenSafeNative]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
}
exit 0
POWERSHELL
}
# <<< windows-cli-shim <<<

if ! ts_resolve_python; then
    ts_print_no_python_guidance
    exit 1
fi
PYTHON_BIN="${TS_PYTHON}"

if ! ts_bootstrap_dependencies "${PYTHON_BIN}" "${VENV_DIR}"; then
    exit 1
fi
PYTHON_BIN="${TS_PYTHON}"

echo "  Making CLI executable..."
chmod +x "${SCRIPT_DIR}/bin/tokensafe"
chmod +x "${SCRIPT_DIR}/bin/agy-tokens"

if ts_is_windows; then
    ts_install_windows_cmd_shim "${PYTHON_BIN}" "${SCRIPT_DIR}/bin/tokensafe" "${HOME}/.local/bin"
fi

echo ""
echo "✅ TokenSafe successfully installed!"
echo "   Plugin location: ${TARGET_DIR}"
echo "   Interpreter: ${PYTHON_BIN}"
echo "   CLI tool: tokensafe (or ${SCRIPT_DIR}/bin/tokensafe)"
echo ""
echo "Try running:"
echo "   tokensafe stats"
echo "   tokensafe test \"Please rewrite the get_balance function in wallet.ts\""
