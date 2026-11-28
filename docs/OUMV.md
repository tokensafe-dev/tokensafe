# OUMV in TokenSafe — supported stacks and how wiring works

OUMV (Observe · Understand · Mutate · Verify) is served by **adapters for the stack you
already use**. There is no TokenSafe framework to adopt: the native SolidJS runtime and
the `tokensafe.New()` Go framework still exist in the trees for tests, but they are not
the recommended path and are not documented as a product surface.

## Supported stacks

| Profile | Stack | What gets mounted | Activation | Repo footprint |
| :--- | :--- | :--- | :--- | :--- |
| Frontend | React 18/19 + Vite | JSON-RPC at `/__oumv/rpc` on the dev server (L0–L4, contracts, OCC edits) | `tokensafe dev` | **none** |
| Frontend | Vue 3 + Vite | Same endpoint; DOM-level L0–L2 | `tokensafe dev` | **none** |
| Backend | Python / FastAPI | `mount_tokensafe(app)` via `uvicorn` import hook | `tokensafe run-backend uvicorn ...` | **none** |
| Backend | Go / Gin | `oumvgin.AutoMount(r)` | `tokensafe inject` | one line + one import (marker-delimited, revertible) **and** `go.mod`/`go.sum` dependency entries |
| Backend | Go / Chi | `oumvchi.AutoMount(r)` | `tokensafe inject` | same |
| Backend | Go / net/http | `stdhttp.NewServeMux(rt)` | generated project (`tokensafe create --template gin`) | adapter-owned mux |
| Backend | Node.js / Express, Fastify | `mountTokensafe(app)` / `registerTokensafe(app)` | `tokensafe inject` | one line (marker-delimited, revertible) **and** npm dependency |
| Backend | Next.js (App Router) | `createNextRouteHandlers()` Route Handler | `tokensafe inject` | one generated `app/api/tokensafe/route.ts` |
| Backend | Elysia (Bun) | `app.use(oumv())` from `tokensafe-backend-node/elysia` | `tokensafe inject` | one line (marker-delimited, revertible) **and** `bun add tokensafe-backend-node` |

Anything else (Django, Angular, Rails, …) is explicitly unsupported: the routing brief
tells the agent to use `repo_map` / `scoped_search` / `smart_read_file` there instead of
probing OUMV.

## Commands

```bash
tokensafe inject --status     # plan: what is wired, what is missing, exact commands
tokensafe inject --dry-run    # same plan, explicit
tokensafe inject              # apply: config + Python autoload + Go one-liner
tokensafe inject --revert     # restore the Go entrypoint byte-for-byte
tokensafe dev                 # zero-footprint Vite bridge for the frontend
tokensafe run-backend uvicorn app:app --reload   # FastAPI with the autoload hook
tokensafe oumv doctor         # readiness + blocking reasons
tokensafe create shop --template react-fastapi   # greenfield, already wired
```

## Files that are written, and where

| Path | Purpose | In git? |
| :--- | :--- | :--- |
| `~/.tokensafe/runtimes/<hash>.json` | discovery descriptor (url, session token, levels) | outside the repo |
| `~/.tokensafe/pyhooks/sitecustomize.py` | uvicorn import hook for FastAPI | outside the repo |
| `~/.tokensafe/inject/<hash>/` | Go entrypoint backup + manifest | outside the repo |
| `<repo>/.tokensafe/oumv.json` | project config (permissions, adapters) | excluded via `.git/info/exclude` |
| `<repo>/main.go` (Go only) | one `AutoMount()` line + import | **visible**, revertible |
| `<repo>/go.mod`, `go.sum` (Go only) | `go get` + `go mod tidy` for the adapter module | **visible** (normal dependency commit) |

The Go edits are the only tracked changes, and they are unavoidable: an adapter must live
inside the process, and its module must be in the dependency graph. It is inserted between `// tokensafe:oumv:begin` / `// tokensafe:oumv:end`,
validated with `go build ./...`, and `tokensafe inject --revert` restores the original
bytes from the backup.

## What the agent is told

On the first prompt in a workspace, hooks (Antigravity, Claude Code, ZCode, Kimi) and the
MCP server's `initialize.instructions` receive a short routing brief:

```
[TokenSafe OUMV routing brief]
- tools: tokensafe:oumv(action='status'|'observe'|'understand'|'verify'|'mutate'|'graph'|'index'|'preview'|'build', workspace_dir='<repo>') — wire 1.6.0; index/preview/build are honest-first (index works offline on the static graph, preview/build need a live runtime and say so otherwise).
- scope: per-root verdicts below; for any unlisted repo call tokensafe:oumv(action='status', workspace_dir='<repo>').
- frontend react/react: OUMV live via `tokensafe dev` ... Call tokensafe:oumv(...)
- backend fastapi/fastapi: OUMV auto-mounts via `tokensafe run-backend uvicorn ...`
- other workspace roots:
- /abs/tokensafe-site: frontend react/react — OUMV ready; tokensafe:oumv(workspace_dir='/abs/tokensafe-site') for status/observe/mutate.
- mutations: enabled with guardrails (OCC version, path jail, syntax pre-flight, backups)
```

Every open folder is classified separately: hosts that report several roots (ZCode
`workspacePaths`) and `TOKENSAFE_WORKSPACES` are routed one by one, and a single-root
brief states its scope explicitly. A verdict for one repository never describes another —
an agent that edits a repo outside the listed roots must call
`tokensafe:oumv(action='status', workspace_dir='<repo>')` first.

Cursor and Codex have no injectable hook — they receive the same text through MCP
instructions. Nothing is committed to the repository for the brief.

## Verifying

```bash
tokensafe oumv doctor                 # config + adapter presence + blocking reasons
curl -s localhost:5173/__oumv/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"oumv/handshake","params":{}}'
```

Conformance reports per adapter: `tokensafe-backend/certification/*.json` (Go + FastAPI,
Level 3, reproduced in CI).
