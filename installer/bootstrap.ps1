<#
.SYNOPSIS
    TokenSafe Windows bootstrapper: provisions the runtime and creates the CLI shims.

.DESCRIPTION
    The .exe installer copies the engine tree and runs this script. It is the
    PowerShell twin of the `bootstrap-python` block in install.sh, with one
    addition the shell scripts cannot offer: a machine with neither CPython nor
    Node still gets a working install, because the pinned CPython is downloaded
    here with .NET instead of the Node launcher.

    Modes:
      Provision  runtime + cryptography + tokensafe.cmd (used by the installer)
      Setup      Provision, then `tokensafe setup --all` (non-interactive agents)
      SmokeTest  Provision only; used by CI to prove the download and the pin

    A failure is written to <TokenSafeHome>\install.log and rethrown, so the
    installer can show the log path instead of a bare "unknown error".
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [ValidateSet('Provision', 'Setup', 'SmokeTest')][string]$Mode = 'Provision',
    [string]$TokenSafeHome = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not $TokenSafeHome) {
    $TokenSafeHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.tokensafe'
}
$Script:LogPath = Join-Path $TokenSafeHome 'install.log'
$Script:Python = ''

function Write-Step {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host "  $Message"
    $parent = Split-Path -Parent $Script:LogPath
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Add-Content -Path $Script:LogPath -Value $line -Encoding UTF8
}

function Test-CPython {
    # Runs the interpreter's own probe; returns its resolved path or $null.
    param([string[]]$Command)
    $arguments = @()
    if ($Command.Count -gt 1) { $arguments = $Command[1..($Command.Count - 1)] }
    $arguments += @('-c', 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)')
    $result = Invoke-NativeCapture -File $Command[0] -Arguments $arguments
    if ($result.ExitCode -ne 0) { return $null }
    $lines = @($result.Output | Where-Object { "$_".Trim() })
    if (-not $lines) { return $null }
    return ($lines | Select-Object -Last 1).ToString().Trim()
}

# Native tools write diagnostics to stderr, and Windows PowerShell 5.1 turns any
# redirected stderr into a terminating error while $ErrorActionPreference is
# 'Stop' - which is how a plain `import cryptography` probe crashed the whole
# installer run. Every native call goes through these helpers instead.
function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $File @Arguments *>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $previous
    }
    return $LASTEXITCODE
}

function Invoke-NativeCapture {
    param([string]$File, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $File @Arguments *>&1
    } finally {
        $ErrorActionPreference = $previous
    }
    return @{ ExitCode = $LASTEXITCODE; Output = @($output) }
}

function Resolve-BasePython {
    if ($env:TOKENSAFE_PYTHON) {
        $pinned = Test-CPython -Command @($env:TOKENSAFE_PYTHON)
        if (-not $pinned) { throw "TOKENSAFE_PYTHON=$($env:TOKENSAFE_PYTHON) does not run CPython 3.10+." }
        return $pinned
    }

    # CI must exercise the download path even though the runner has a suitable
    # CPython: without this the provisioning code is never tested end to end.
    if ($env:TOKENSAFE_FORCE_MANAGED_RUNTIME -eq '1') { return $null }

    $candidates = New-Object System.Collections.ArrayList
    # A CPython the npm launcher already provisioned needs no download.
    $runtimeRoot = Join-Path $TokenSafeHome 'runtime'
    if (Test-Path $runtimeRoot) {
        Get-ChildItem -Path $runtimeRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = Join-Path $_.FullName 'python\python.exe'
            if (Test-Path $exe) { [void]$candidates.Add(@($exe)) }
        }
    }
    foreach ($name in @('py', 'python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        # The Microsoft Store App Execution Alias is a placeholder that exits 9009
        # and would open the Store; it can never be an interpreter.
        if ($command.Source -match 'WindowsApps') { continue }
        if ($name -eq 'py') { [void]$candidates.Add(@('py', '-3')) } else { [void]$candidates.Add(@($command.Source)) }
    }

    foreach ($candidate in $candidates) {
        $resolved = Test-CPython -Command $candidate
        if ($resolved) { return $resolved }
    }
    return $null
}

function Test-Cryptography {
    param([string]$Python)
    $result = Invoke-NativeCapture -File $Python -Arguments @('-c', 'import cryptography')
    return ($result.ExitCode -eq 0)
}

function Install-Cryptography {
    param([string]$Python)
    Write-Step "Installing cryptography>=42.0 into $Python"
    $code = Invoke-Native -File $Python -Arguments @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', 'cryptography>=42.0')
    if (Test-Cryptography -Python $Python) { return $true }
    Write-Step "pip exited with $code and cryptography is still missing; trying a private venv"
    # A locked-down interpreter (PEP 668, read-only Store install) gets a venv.
    $venv = Join-Path $TokenSafeHome 'venv'
    if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
        Invoke-Native -File $Python -Arguments @('-m', 'venv', $venv) | Out-Null
    }
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        Invoke-Native -File $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', 'cryptography>=42.0') | Out-Null
        if (Test-Cryptography -Python $venvPython) {
            $Script:Python = $venvPython
            return $true
        }
    }
    return $false
}

function Get-ManagedPython {
    $manifestPath = Join-Path $AppDir 'bin\python-runtime.json'
    if (-not (Test-Path $manifestPath)) { throw "Missing runtime manifest: $manifestPath" }
    $manifest = Get-Content -Path $manifestPath -Raw | ConvertFrom-Json

    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw "No pinned CPython build for $($env:PROCESSOR_ARCHITECTURE); install Python 3.10+ from python.org."
    }
    $target = $manifest.targets.'win32-x64'
    if (-not $target) { throw 'The runtime manifest has no win32-x64 target.' }

    $runtimeDir = Join-Path $TokenSafeHome ("runtime\{0}-{1}" -f $manifest.python, $manifest.release)
    $marker = Join-Path $runtimeDir '.complete'
    $existing = Join-Path $runtimeDir 'python\python.exe'
    if ((Test-Path $marker) -and (Test-Path $existing)) {
        $recorded = (Get-Content $marker -TotalCount 1).Trim().ToLower()
        if ($recorded -eq $target.sha256.ToLower()) {
            Write-Step "Managed CPython $($manifest.python) already present"
            return $existing
        }
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = '{0}/{1}/{2}' -f $manifest.base_url, $manifest.release, $target.asset
    $archive = Join-Path $env:TEMP $target.asset
    Write-Step "Downloading managed CPython $($manifest.python) (~$([math]::Round($target.size / 1MB)) MB)"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive

    $digest = (Get-FileHash -Path $archive -Algorithm SHA256).Hash.ToLower()
    if ($digest -ne $target.sha256.ToLower()) {
        Remove-Item -Path $archive -Force -ErrorAction SilentlyContinue
        throw "SHA-256 mismatch for $($target.asset): got $digest"
    }

    Write-Step "Extracting into $runtimeDir"
    if (Test-Path $runtimeDir) { Remove-Item -Path $runtimeDir -Recurse -Force }
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    # Windows 10+ ships bsdtar; it handles .tar.gz and keeps the `python/` layout
    # the npm launcher expects (RUNTIME_PYTHON_LAYOUT in bin/tokensafe.js).
    $code = Invoke-Native -File 'tar.exe' -Arguments @('-xzf', $archive, '-C', $runtimeDir)
    if ($code -ne 0) { throw "tar.exe failed to extract $archive (exit $code)" }
    Remove-Item -Path $archive -Force -ErrorAction SilentlyContinue

    $python = Join-Path $runtimeDir 'python\python.exe'
    if (-not (Test-Path $python)) { throw 'The runtime archive did not contain python\python.exe' }
    # Single line: bin/tokensafe.js compares split("\n")[0] against the pin, so a
    # second line would leave a stray CR on the digest.
    Set-Content -Path $marker -Value $target.sha256 -Encoding ASCII
    Write-Step "Managed CPython ready at $python"
    return $python
}

function New-CliShims {
    param([string]$Python)
    $bin = Join-Path $AppDir 'bin'
    $cli = Join-Path $bin 'tokensafe'
    $content = (@(
        '@echo off',
        ('"{0}" "{1}" %*' -f $Python, $cli)
    ) -join "`r`n") + "`r`n"
    Set-Content -Path (Join-Path $bin 'tokensafe.cmd') -Value $content -Encoding ASCII
    Set-Content -Path (Join-Path $bin 'agy-tokens.cmd') -Value $content -Encoding ASCII
    Write-Step "Created $bin\tokensafe.cmd"
}

try {
    if (-not (Test-Path (Join-Path $AppDir 'bin\tokensafe'))) {
        throw "Not a TokenSafe tree: $AppDir"
    }
    New-Item -ItemType Directory -Path $TokenSafeHome -Force | Out-Null
    Write-Step "TokenSafe bootstrapper: mode=$Mode home=$TokenSafeHome"

    $base = Resolve-BasePython
    if ($base) {
        Write-Step "Using CPython at $base"
        $Script:Python = $base
        if (Test-Cryptography -Python $base) {
            Write-Step 'cryptography already importable'
        } elseif (-not (Install-Cryptography -Python $base)) {
            throw "Could not install cryptography into $base. Fix: `"$base`" -m pip install cryptography>=42.0"
        }
    } else {
        Write-Step 'No CPython 3.10+ found; provisioning the pinned build'
        $Script:Python = Get-ManagedPython
        if (-not (Test-Cryptography -Python $Script:Python)) {
            if (-not (Install-Cryptography -Python $Script:Python)) {
                throw "Could not install cryptography into $Script:Python"
            }
        }
    }

    New-CliShims -Python $Script:Python

    if ($Mode -eq 'Setup') {
        Write-Step 'Configuring detected agents (tokensafe setup --all)'
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $Script:Python (Join-Path $AppDir 'bin\tokensafe') setup --all
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -ne 0) { throw "tokensafe setup --all exited with $LASTEXITCODE" }
    }

    Write-Step 'Done'
    exit 0
} catch {
    $message = $_.Exception.Message
    try {
        Add-Content -Path $Script:LogPath -Value ('ERROR {0}' -f $message) -Encoding UTF8
    } catch {
        # the console message below still reaches the installer's log
    }
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}
