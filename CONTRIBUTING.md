# Contributing to TokenSafe

Thank you for your interest in contributing to TokenSafe! We welcome contributions to improve token efficiency, agent hooks, AST parsers, and Model Context Protocol (MCP) tooling.

---

## ✍️ Sign-off (DCO)

This project uses the **Developer Certificate of Origin 1.1** ([DCO](DCO)). Sign every commit:

```bash
git commit -s -m "fix(search): ..."
```

The sign-off adds `Signed-off-by: Your Name <you@example.com>`; PRs without it are asked to amend. There is no separate CLA. The engine is Apache-2.0 from 4.7.0 (releases ≤4.6.4 stay MIT), and contributions are accepted under the same terms.

---

## 🎯 Core Philosophy & Guidelines

1. **Minimal Runtime Dependencies (`cryptography>=42.0` required, AST grammars optional)**:
   - Core AST, proxy, MCP, guard, and SQLite modules run strictly on the Python standard library; `cryptography>=42.0` (`pyproject.toml` / `requirements.txt`) is the single *required* runtime dependency for Ed25519/RS256 cryptographic verification.
   - `tree-sitter` plus grammar wheels and `Pillow` are optional accelerators. `src/treesitter_backend.py` and `src/image_downscale.py` must keep their stdlib fallbacks working when those packages are absent.
   - Do not add other third-party runtime dependencies without approval.
2. **Deterministic Token Economy**:
   - Every modification to compactor, slicer, or AST skeletonizer must be benchmarked for token savings and verified for zero code semantics loss.
3. **Comprehensive Unit Tests**:
   - Every new feature, hook, or bug fix must include corresponding tests under `tests/`.

---

## 🛠️ Local Development Setup

Clone the repository and verify your Python environment:

```bash
git clone https://github.com/tokensafe-dev/tokensafe.git
cd tokensafe

# Verify Python version (requires Python 3.10+)
python3 --version
```

### Running Tests

Execute the full test suite using Python's standard `unittest`:

```bash
python3 -m unittest discover tests
```

To run a single test module:

```bash
python3 -m unittest tests/test_compactor.py
```

### Testing the CLI

```bash
# Run local standalone Python binary
./bin/tokensafe --help

# Test Node.js launcher
node ./bin/tokensafe.js --help
```

---

## 🔀 Submitting a Pull Request

1. **Fork and branch**: Create a descriptive feature branch (`feat/new-slicer` or `fix/mcp-schema-parsing`).
2. **Write tests**: Add unit tests for your changes in `tests/`.
3. **Ensure tests pass**: Run `python3 -m unittest discover tests`.
4. **Open a PR**: Fill out the PR template with a summary of changes, benchmark numbers (if applicable), and testing steps.

---

## 💬 Community & Questions

- Report bugs or request features via [GitHub Issues](https://github.com/tokensafe-dev/tokensafe/issues).
- For security vulnerabilities, refer to [SECURITY.md](SECURITY.md).
