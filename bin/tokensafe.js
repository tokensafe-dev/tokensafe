#!/usr/bin/env node

/**
 * TokenSafe - Universal Node.js CLI & MCP Launcher
 * Works with `npx tokensafe [command]` or global `tokensafe [command]`
 */

const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const https = require('https');

const ROOT_DIR = path.resolve(__dirname, '..');
const args = process.argv.slice(2);

const TOKENSAFE_HOME = process.env.TOKENSAFE_HOME
  ? path.resolve(process.env.TOKENSAFE_HOME)
  : path.join(os.homedir(), '.tokensafe');
const VENV_DIR = path.join(TOKENSAFE_HOME, 'venv');
const RUNTIME_ROOT = path.join(TOKENSAFE_HOME, 'runtime');
const MANIFEST_PATH = path.join(__dirname, 'python-runtime.json');
const MIN_PYTHON = [3, 10];

// Only `cryptography` is required: license verification imports it on every
// guarded call. tree-sitter grammars and Pillow are accelerators - the AST
// backend falls back to stdlib heuristics when they are missing.
const REQUIRED_DEPS = ['cryptography>=42.0'];
const OPTIONAL_DEPS = [
  'tree-sitter>=0.23',
  'tree-sitter-python',
  'tree-sitter-javascript',
  'tree-sitter-typescript',
  'tree-sitter-go',
  'tree-sitter-rust',
  'tree-sitter-java',
  'tree-sitter-php',
  'tree-sitter-kotlin',
  'tree-sitter-c-sharp',
  'tree-sitter-c',
  'tree-sitter-cpp',
  'tree-sitter-ruby',
  'tree-sitter-swift',
  'Pillow>=10.0.0'
];

function note(message) {
  console.error(`⚡ TokenSafe: ${message}`);
}

function fail(message) {
  console.error(`\n❌ ${message}\n`);
  process.exit(1);
}

function loadManifest() {
  try {
    return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  } catch (_) {
    return null;
  }
}

function venvPythonPath() {
  return process.platform === 'win32'
    ? path.join(VENV_DIR, 'Scripts', 'python.exe')
    : path.join(VENV_DIR, 'bin', 'python3');
}

/**
 * Returns the interpreter's own path when it is a usable CPython >= 3.10,
 * optionally requiring importable runtime dependencies. null otherwise.
 */
function probePython(cmd, requireDeps) {
  const imports = requireDeps ? 'import sys, cryptography' : 'import sys';
  const probe = `${imports}; assert sys.version_info >= (${MIN_PYTHON[0]}, ${MIN_PYTHON[1]}); print(sys.executable)`;
  try {
    const res = spawnSync(cmd, ['-c', probe], { encoding: 'utf-8', stdio: 'pipe', timeout: 20000 });
    if (res.status !== 0) return null;
    return (res.stdout || '').trim() || cmd;
  } catch (_) {
    return null;
  }
}

function moduleAvailable(cmd, name) {
  try {
    return spawnSync(cmd, ['-c', `import ${name}`], { stdio: 'pipe', timeout: 20000 }).status === 0;
  } catch (_) {
    return false;
  }
}

function pipInstall(python, packages, extraArgs) {
  if (!packages.length) return true;
  const res = spawnSync(
    python,
    ['-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-warn-script-location', ...(extraArgs || []), ...packages],
    { encoding: 'utf-8', stdio: 'pipe', timeout: 900000 }
  );
  return res.status === 0;
}

/**
 * Installs dependencies for an interpreter that exists but lacks them.
 * `isolated` targets the interpreter's own prefix (managed runtime / venv),
 * otherwise pip --user keeps the system interpreter untouched.
 */
function installDeps(python, isolated) {
  const scope = isolated ? [] : ['--user'];
  if (!pipInstall(python, REQUIRED_DEPS, scope)) {
    if (!probePython(python, true)) return false;
  }
  if (!pipInstall(python, OPTIONAL_DEPS, scope)) {
    note('optional accelerators (tree-sitter grammars, Pillow) were not installed — stdlib fallbacks stay active');
  }
  return probePython(python, true) !== null;
}

function bootstrapDependencies(basePython) {
  note('installing Python dependencies (cryptography, tree-sitter grammars)...');
  if (installDeps(basePython, false)) return basePython;

  // PEP 668 externally-managed environments (macOS Homebrew, modern Ubuntu)
  // reject --user installs; fall back to a private venv.
  fs.mkdirSync(path.dirname(VENV_DIR), { recursive: true });
  const venvPy = venvPythonPath();
  if (!fs.existsSync(venvPy)) {
    const mk = spawnSync(basePython, ['-m', 'venv', VENV_DIR], { encoding: 'utf-8', stdio: 'pipe', timeout: 300000 });
    if (mk.status !== 0) return null;
  }
  return installDeps(venvPy, true) ? venvPy : null;
}

function platformKey() {
  const arch = process.arch === 'arm64' ? 'arm64' : process.arch === 'x64' ? 'x64' : null;
  if (!arch) return null;
  if (process.platform === 'darwin') return `darwin-${arch}`;
  if (process.platform === 'win32') return arch === 'x64' ? 'win32-x64' : null;
  if (process.platform === 'linux') {
    if (!hasGlibc()) return null; // Alpine/musl: no compatible build pinned
    return arch === 'x64' ? 'linux-x64' : arch === 'arm64' ? 'linux-arm64' : null;
  }
  return null;
}

function hasGlibc() {
  try {
    const report = process.report && process.report.getReport ? process.report.getReport() : null;
    if (report && report.header) return Boolean(report.header.glibcVersionRuntime);
  } catch (_) {
    /* fall through */
  }
  try {
    return fs.existsSync('/lib/ld-linux-x86-64.so.2') || fs.existsSync('/lib/ld-linux-aarch64.so.1');
  } catch (_) {
    return false;
  }
}

const RUNTIME_PYTHON_LAYOUT = [
  ['python', 'bin', 'python3'],
  ['python', 'bin', 'python'],
  ['python', 'python.exe'],
  ['bin', 'python3'],
  ['python3']
];

/** Removes runtime directories this launcher created for an older pin. */
function pruneStaleRuntimes(keepDir) {
  try {
    for (const entry of fs.readdirSync(RUNTIME_ROOT)) {
      const candidate = path.join(RUNTIME_ROOT, entry);
      if (candidate === keepDir) continue;
      if (!fs.existsSync(path.join(candidate, '.complete'))) continue;
      if (fs.statSync(candidate).isDirectory()) fs.rmSync(candidate, { recursive: true, force: true });
    }
  } catch (_) {
    /* best effort */
  }
}

function findRuntimePython(dir) {
  for (const parts of RUNTIME_PYTHON_LAYOUT) {
    const candidate = path.join(dir, ...parts);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(file);
    stream.on('error', reject);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

function download(url, dest, expectedSize, redirects) {
  const hops = redirects || 0;
  return new Promise((resolve, reject) => {
    const request = https.get(url, { headers: { 'user-agent': 'tokensafe-launcher' }, timeout: 120000 }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode)) {
        res.resume();
        if (hops >= 6 || !res.headers.location) return reject(new Error('too many redirects'));
        return download(res.headers.location, dest, expectedSize, hops + 1).then(resolve, reject);
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }

      const total = Number(res.headers['content-length'] || expectedSize || 0);
      let received = 0;
      let lastReported = -1;
      const out = fs.createWriteStream(dest);

      res.on('data', (chunk) => {
        received += chunk.length;
        if (total > 0 && process.stderr.isTTY) {
          const pct = Math.floor((received / total) * 100);
          if (pct >= lastReported + 10) {
            lastReported = pct;
            const mb = (n) => Math.round(n / 1048576);
            process.stderr.write(`\r⚡ TokenSafe: downloading managed CPython... ${pct}% (${mb(received)}/${mb(total)} MB)`);
          }
        }
      });
      res.pipe(out);
      res.on('error', reject);
      out.on('error', reject);
      out.on('finish', () => {
        if (process.stderr.isTTY && total > 0) process.stderr.write('\r\x1b[K');
        resolve(received);
      });
    });
    request.on('timeout', () => request.destroy(new Error('download timed out')));
    request.on('error', reject);
  });
}

function extractArchive(archive, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  const res = spawnSync('tar', ['-xzf', archive, '-C', destDir], { encoding: 'utf-8', stdio: 'pipe', timeout: 600000 });
  if (res.status !== 0) {
    const detail = ((res.stderr || '') || (res.error && res.error.message) || '').trim();
    throw new Error(`failed to extract the runtime archive${detail ? `: ${detail}` : ''}`);
  }
}

async function provisionManagedRuntime(manifest, target, dir) {
  fs.mkdirSync(RUNTIME_ROOT, { recursive: true });
  const url = `${manifest.base_url}/${manifest.release}/${encodeURIComponent(target.asset)}`;
  const archive = path.join(RUNTIME_ROOT, `.${target.asset}.part`);

  note(`no Python 3.10+ found — provisioning managed CPython ${manifest.python} (~${Math.round((target.size || 0) / 1048576)} MB, one time)`);

  try {
    const received = await download(url, archive, target.size);
    if (target.size && received !== target.size) {
      throw new Error(`truncated download (${received} of ${target.size} bytes)`);
    }
    const digest = await sha256File(archive);
    if (digest.toLowerCase() !== target.sha256.toLowerCase()) {
      throw new Error(`SHA-256 mismatch for ${target.asset} (got ${digest}, expected ${target.sha256})`);
    }

    if (fs.existsSync(dir) && dir.startsWith(RUNTIME_ROOT + path.sep)) {
      fs.rmSync(dir, { recursive: true, force: true });
    }
    extractArchive(archive, dir);
    fs.writeFileSync(path.join(dir, '.complete'), `${target.sha256}\n${target.asset}\n`);

    const python = findRuntimePython(dir);
    if (!python) throw new Error('runtime archive did not contain a python interpreter');

    if (!probePython(python, false)) {
      throw new Error(`the downloaded interpreter does not run on this system (${platformKey()})`);
    }

    if (!probePython(python, true) && !installDeps(python, true)) {
      throw new Error('could not install cryptography into the managed runtime');
    }

    const grammars = moduleAvailable(python, 'tree_sitter')
      ? 'with tree-sitter grammars'
      : 'stdlib fallbacks only (tree-sitter unavailable)';
    pruneStaleRuntimes(dir);
    note(`managed CPython ${manifest.python} ready at ${python} — ${grammars}`);
    return python;
  } finally {
    try {
      if (fs.existsSync(archive)) fs.unlinkSync(archive);
    } catch (_) {
      /* best effort */
    }
  }
}

async function ensureManagedRuntime() {
  const manifest = loadManifest();
  if (!manifest) return null;

  const key = platformKey();
  const target = key && manifest.targets ? manifest.targets[key] : null;
  if (!target) {
    note(`no pinned CPython build for ${process.platform}-${process.arch}`);
    return null;
  }

  const dir = path.join(RUNTIME_ROOT, `${manifest.python}-${manifest.release}`);
  const marker = path.join(dir, '.complete');
  const existing = findRuntimePython(dir);
  if (existing && fs.existsSync(marker)) {
    let markerOk = false;
    try {
      markerOk = fs.readFileSync(marker, 'utf8').trim().split('\n')[0].toLowerCase() === target.sha256.toLowerCase();
    } catch (_) {
      markerOk = false;
    }
    if (markerOk && probePython(existing, false)) {
      if (!probePython(existing, true) && !installDeps(existing, true)) return null;
      return existing;
    }
  }

  if (String(process.env.TOKENSAFE_NO_DOWNLOAD || '').trim() === '1') {
    note('TOKENSAFE_NO_DOWNLOAD=1 — skipping the managed CPython download');
    return null;
  }

  try {
    return await provisionManagedRuntime(manifest, target, dir);
  } catch (err) {
    note(`managed runtime provisioning failed: ${err.message}`);
    return null;
  }
}

function printInstallGuidance() {
  console.error('\n❌ TokenSafe requires CPython 3.10+ and no managed runtime could be provisioned.');
  console.error('Install Python once, then re-run `tokensafe`:');
  console.error('  macOS:          brew install python3');
  console.error('  Debian/Ubuntu:  sudo apt-get install python3 python3-venv');
  console.error('  Fedora:         sudo dnf install python3');
  console.error('  Windows:        winget install Python.Python.3.12');
  console.error('  Any platform:   https://www.python.org/downloads/\n');
  console.error('Or point TokenSafe at an existing interpreter:');
  console.error('  TOKENSAFE_PYTHON=/path/to/python3 tokensafe\n');
  console.error('Managed runtime downloads can be disabled: TOKENSAFE_NO_DOWNLOAD=1\n');
}

async function findPython(options = {}) {
  const { allowProvision = true, requireDeps = true } = options;
  const pinned = (process.env.TOKENSAFE_PYTHON || '').trim();
  if (pinned) {
    if (probePython(pinned, true)) return pinned;
    if (requireDeps && probePython(pinned, false)) {
      const bootstrapped = bootstrapDependencies(pinned);
      if (bootstrapped) return bootstrapped;
    }
    if (!requireDeps && probePython(pinned, false)) return pinned;
    fail(`TOKENSAFE_PYTHON=${pinned} is not a usable CPython ${MIN_PYTHON[0]}.${MIN_PYTHON[1]}+ with 'cryptography'.`);
  }

  const candidates = [];
  const venvPy = venvPythonPath();
  if (fs.existsSync(venvPy)) candidates.push(venvPy);
  candidates.push('python3', 'python');

  if (!requireDeps) {
    // `help` only prints: a bare interpreter is enough, and a dependency
    // bootstrap there would turn a two-line reference into a pip install.
    for (const cmd of candidates) {
      if (probePython(cmd, false)) return cmd;
    }
    return allowProvision ? await ensureManagedRuntime() : null;
  }

  for (const cmd of candidates) {
    if (probePython(cmd, true)) return cmd;
  }
  for (const cmd of candidates) {
    if (probePython(cmd, false)) {
      const bootstrapped = bootstrapDependencies(cmd);
      if (bootstrapped) return bootstrapped;
    }
  }

  return allowProvision ? await ensureManagedRuntime() : null;
}

const firstArg = args[0] || '';
const wantsHelp = firstArg === 'help' || firstArg === '--help' || firstArg === '-h';

// The command reference lives in src/cli.py:_print_help(). This launcher used
// to carry its own copy and fell behind: `dev`, `inject`, `adapters` and
// `bench-oumv` were missing, so `tokensafe help` advertised a subset of the
// real dispatcher. Help is forwarded to the Python CLI now, so one list feeds
// both entrypoints. This fallback only prints when no interpreter exists yet.
function printHelpFallback() {
  console.log(`
⚡ TokenSafe - Local-First AI Coding Guardrails & Context Optimizer
Website: https://tokensafe.dev
Docs:    https://tokensafe.dev/docs

The command reference is printed by the Python CLI, which is not available yet.
Install CPython ${MIN_PYTHON[0]}.${MIN_PYTHON[1]}+ once and rerun 'tokensafe help':
  macOS:          brew install python3
  Debian/Ubuntu:  sudo apt-get install python3 python3-venv
  Fedora:         sudo dnf install python3
  Windows:        winget install Python.Python.3.12

No interpreter at hand? The launcher can provision a pinned build on the first
real command (network required), or accept an existing one:
  TOKENSAFE_PYTHON=/path/to/python3 tokensafe help
`);
}

async function main() {
  // Help must never trigger the managed-runtime download: it is a print, not a
  // run. Probe what is already installed; the fallback covers the rest.
  const python = await findPython({ allowProvision: !wantsHelp, requireDeps: !wantsHelp });
  if (!python) {
    if (wantsHelp) {
      printHelpFallback();
      process.exit(0);
    }
    printInstallGuidance();
    process.exit(1);
  }

  const candidateTokensafe = path.join(ROOT_DIR, 'bin', 'tokensafe');
const candidateAgy = path.join(ROOT_DIR, 'bin', 'agy-tokens');
const scriptPath = fs.existsSync(candidateTokensafe) ? candidateTokensafe : candidateAgy;
if (!fs.existsSync(scriptPath)) {
  console.error(`Error: TokenSafe script not found at ${scriptPath}`);
  process.exit(1);
}

const scriptArgs = args.length === 0 ? ['install'] : args;

const child = spawn(python, [scriptPath, ...scriptArgs], {
  stdio: 'inherit',
  cwd: process.cwd(),
  env: {
    ...process.env,
    PYTHONPATH: ROOT_DIR + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : '')
  }
});

child.on('error', (err) => {
  console.error(`TokenSafe: failed to launch Python CLI: ${err.message}`);
  process.exit(1);
});

  child.on('exit', (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
    } else {
      process.exit(code || 0);
    }
  });
}

if (require.main === module) {
  main().catch((err) => {
    console.error(`TokenSafe: ${err && err.message ? err.message : err}`);
    process.exit(1);
  });
} else {
  module.exports = {
    TOKENSAFE_HOME,
    RUNTIME_ROOT,
    REQUIRED_DEPS,
    OPTIONAL_DEPS,
    loadManifest,
    platformKey,
    probePython,
    findRuntimePython,
    sha256File,
    installDeps,
    ensureManagedRuntime,
    findPython
  };
}
