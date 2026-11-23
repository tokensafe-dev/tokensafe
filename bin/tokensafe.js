#!/usr/bin/env node

/**
 * TokenSafe - Universal Node.js CLI & MCP Launcher
 * Works with `npx tokensafe [command]` or global `tokensafe [command]`
 */

const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const ROOT_DIR = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const VENV_DIR = path.join(os.homedir(), '.tokensafe', 'venv');
const CORE_DEPS = [
  'cryptography>=42.0',
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

function checkPython(cmd, requireDeps) {
  const probe = requireDeps
    ? 'import sys, cryptography, tree_sitter; assert sys.version_info >= (3, 10)'
    : 'import sys; assert sys.version_info >= (3, 10)';
  try {
    const res = spawnSync(cmd, ['-c', probe], { encoding: 'utf-8', stdio: 'pipe' });
    return res.status === 0;
  } catch (_) {
    return false;
  }
}

function bootstrapDependencies(basePython) {
  console.error('⚡ Installing TokenSafe dependencies (cryptography & tree-sitter AST grammars)...');
  // 1. Try user-site pip first (respects normal user environments)
  const userPip = spawnSync(
    basePython,
    ['-m', 'pip', 'install', '--user', '--quiet', '--disable-pip-version-check', ...CORE_DEPS],
    { encoding: 'utf-8', stdio: 'pipe' }
  );
  if (userPip.status === 0 && checkPython(basePython, true)) {
    return basePython;
  }

  // 2. Fallback to isolated ~/.tokensafe/venv (handles PEP 668 externally-managed Python on macOS/Ubuntu)
  fs.mkdirSync(path.dirname(VENV_DIR), { recursive: true });
  const venvPy = process.platform === 'win32'
    ? path.join(VENV_DIR, 'Scripts', 'python.exe')
    : path.join(VENV_DIR, 'bin', 'python3');

  if (!fs.existsSync(venvPy)) {
    const mk = spawnSync(basePython, ['-m', 'venv', VENV_DIR], { encoding: 'utf-8', stdio: 'pipe' });
    if (mk.status !== 0) return null;
  }
  const venvPip = spawnSync(
    venvPy,
    ['-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', ...CORE_DEPS],
    { encoding: 'utf-8', stdio: 'pipe' }
  );
  if (venvPip.status === 0 && checkPython(venvPy, true)) {
    return venvPy;
  }
  return null;
}

// Locate Python 3.10+ interpreter with required dependencies
function findPython() {
  const venvPy = process.platform === 'win32'
    ? path.join(VENV_DIR, 'Scripts', 'python.exe')
    : path.join(VENV_DIR, 'bin', 'python3');
  const candidates = [venvPy, 'python3', 'python'];

  for (const cmd of candidates) {
    if (cmd === venvPy && !fs.existsSync(venvPy)) continue;
    if (checkPython(cmd, true)) return cmd;
  }

  for (const cmd of candidates) {
    if (cmd === venvPy && !fs.existsSync(venvPy)) continue;
    if (checkPython(cmd, false)) {
      const bootstrapped = bootstrapDependencies(cmd);
      if (bootstrapped) return bootstrapped;
      console.error("\n❌ TokenSafe requires Python 3.10+ and 'cryptography>=42.0'.");
      console.error("Install dependencies manually via:");
      console.error("  pip install 'cryptography>=42.0' 'tree-sitter>=0.23' 'tree-sitter-python'\n");
      process.exit(1);
    }
  }
  return null;
}

const firstArg = args[0] || '';

if (firstArg === 'help' || firstArg === '--help' || firstArg === '-h') {
  console.log(`
⚡ TokenSafe - Local-First AI Coding Guardrails & Context Optimizer
Website: https://tokensafe.dev

Usage:
  tokensafe [command] [options]

Commands:
  tokensafe create <name>      Greenfield project scaffolder (tokensafe-ui + tokensafe-python)
  tokensafe init               Initialize TokenSafe ecosystem in current directory
  tokensafe stats              Show token economy savings & performance metrics
  tokensafe sync               Synchronize local token metrics with Cloud Dashboard
  tokensafe html               Generate and open interactive browser dashboard
  tokensafe test "<prompt>"    Test prompt compression on custom input
  tokensafe expand-ref <hash>  Restore folded tool/command/file result by its 8-char hash
  tokensafe wrap <command>     Run command with compact output folding (e.g. tests/logs)
  tokensafe web <url>          Fetch web page/docs and print clean compact Markdown
  tokensafe doctor             Audit MCP servers & skills context token tax
  tokensafe providers          List active LLM distillation models & keys
  tokensafe benchmark          Run zero-loss verification benchmark suite
  tokensafe mcp                Manage MCP servers (list, disable <name>, enable <name>)
  tokensafe proxy [port]       Start Transparent Local AI Proxy (default: 8765)
  tokensafe graph              Analyze repository God Nodes & dependency topology
  tokensafe map                Generate tokensafe-out/API_SURFACE.md interface snapshot
  tokensafe vacuum             Compact SQLite metrics database storage
  tokensafe prune              Prune granular telemetry events into lifetime rollup
  tokensafe digest             Preview or send weekly team ROI digest email
  tokensafe tests              Run the unit test suite (compact failure report)
  tokensafe activate <key>     Activate commercial Solo or Team seat license key
  tokensafe license            Show current license status and active tier
  tokensafe update             One-click self-update for npm, pip & all AI agent rules
  tokensafe install            Interactive installer for Cursor, Antigravity, Claude, etc.
  tokensafe uninstall          Uninstall TokenSafe from AI agents, unwire proxy & clean rules
  tokensafe --help             Show this help screen
`);
  process.exit(0);
}

const python = findPython();
if (!python) {
  console.error('\n❌ TokenSafe requires Python 3.10+ installed on your system.');
  console.error('Please install Python from https://python.org or via your package manager:');
  console.error('  macOS:  brew install python3');
  console.error('  Debian: sudo apt-get install python3\n');
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
