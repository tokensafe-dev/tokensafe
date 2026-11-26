# OUMV benchmark — Go sandbox (measured pilot)

Status: **pilot**, n = 1 run per arm/task. The harness is real; the numbers are small
and must be read as a smoke test of the methodology, not as a product claim.

- Model: `deepseek-chat` (provider reports it as `deepseek-flash`), temperature 0
- Date: 2026-11-26 · price snapshot: 2026-11-26 (input $0.27/M miss, $0.07/M hit, output $1.10/M)
- Budget: $0.30 hard cap · **spent: $0.043**
- Oracle: the real 12-assertion AUB suite (`go test ./benchmark -run TestGroundTruthOracleVanilla`), re-run after every agent step
- Artifacts: [`docs/bench/go-pilot.json`](bench/go-pilot.json)
- Reproduce: `tokensafe bench-oumv --tasks all --arms baseline,oumv --runs 1 --budget 0.3`

## Methodology

Each session starts from a throwaway copy of `tokensafe-backend/go` with **one deliberate
regression** in `benchmark/sandboxes/vanilla_gin/app.go`. The agent must make the oracle
pass; it is stopped as soon as all 12 assertions are green (`stop=solved`), at 14 steps,
or when the budget is exhausted.

| Arm | Tools |
| :--- | :--- |
| `baseline` | `list_files`, `read_file`, `write_file` (sandbox files only), `run_command` (`go test/build/vet` allowlist) |
| `oumv` | the same four **plus** `oumv_observe`, `oumv_getSource`, `oumv_verify` (request body supported), `oumv_understand` — served by `cmd/benchoumv`, which mounts the OUMV runtime over the same handler |

Both arms edit the same file and are graded by the same oracle. Token counts come from the
provider `usage` payload (cache hit/miss split included); nothing is estimated.

## Results (pilot)

| Arm | Runs | Solved | Steps | Input tokens | Output tokens | Cost |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 3 | **3/3** | 13 | 69,166 | 4,775 | $0.0152 |
| `oumv` | 3 | **2/3** | 36 | 194,457 | 8,419 | $0.0274 |

Per task:

| Task (deliberate regression) | baseline | oumv |
| :--- | :--- | :--- |
| `product-create-status` (201 → 200) | 3 steps · $0.0031 | 8 steps · $0.0054 |
| `order-conflict-status` (409 → 400) | 6 steps · $0.0077 | 14 steps · $0.0112 |
| `product-validation-status` (422 → 200) | 4 steps · $0.0044 | 14 steps · **failed** (0/12, left a build error) |

## What this actually says

1. **The harness works and is honest**: real oracle, real provider tokens, real budget cap,
   per-step oracle snapshots. A run costs ~$0.005–0.011 at DeepSeek prices.
2. **On this target OUMV is not faster — it is slower.** The candidate file is 3.7 KB;
   `grep`/`read_file` already find the regression in 1–2 calls, while the OUMV arm spends
   steps on `observe L1/L2` before editing. The protocol's value (fewer tokens on large
   codebases, in-process `verify` instead of starting a server) cannot show up here.
3. **One failure is a real signal, not noise**: the `oumv` arm left the sandbox in a
   non-compiling state at the step cap. Fewer, larger edits are riskier; the baseline arm's
   small patch won.
4. **A protocol gap surfaced and was fixed**: `oumv/verify` could not POST a body, so the
   OUMV arm could not reproduce the duplicate-key conflict that the baseline reproduced with
   `go test`. `VerifyParams` now carries `payload`/`method`, and `VerifyResult` returns
   canonical `status` + `scope {checked, notExecuted, notSupported}`.

## React sandbox (measured pilot)

The React sandbox was not React until 2026-11-26: its adapter projected markup with
`innerHTML`, so the "React arm" exercised no hooks, no reconciliation and no
stale-closure bugs. It now mounts `ReactTaskManager` with `createRoot`, renders a
synchronous store through `useSyncExternalStore`, and is graded by the same
12-assertion oracle executed headlessly (`src/bin/oracle-runner.ts`).

Pilot (n = 1 per task, `deepseek-chat`, budget $0.20 · **spent $0.0165**):

| Task (deliberate regression) | Oracle before | Oracle after | Steps | Cost |
| :--- | :---: | :---: | ---: | ---: |
| `search-filter` (query ignored) | 10/12 | **12/12** | 7 | $0.0064 |
| `optimistic-rollback` (no rollback on sync failure) | 11/12 | **12/12** | 6 | $0.0060 |
| `saving-indicator` (button never disabled) | 11/12 | **12/12** | 5 | $0.0041 |

Arm: `baseline` only. The OUMV arm for React needs the Vite bridge (`tokensafe dev`)
running against the edited sandbox; that harness is the next step, so this table
does not compare the protocol against anything yet.

Artifacts: [`docs/bench/react-pilot.json`](bench/react-pilot.json)
Reproduce: `tokensafe bench-oumv --target react --tasks all --runs 1 --budget 0.2`

## Next (to make this publishable)

- 3 runs per arm/task (18 sessions Go) with the same budget guard → report mean ± spread.
- A target where context size matters (2–5k LOC service, not a 3.7 KB file), otherwise the
  benchmark measures the harness, not the protocol.
- React target: OUMV arm over the Vite bridge (observe L1/L2, getSource, verify) against the
  same three tasks; only then is a React comparison meaningful.
- Add the TokenSafe-tools arm (scoped search/skeleton) so the three-way split is
  `baseline / tokensafe / oumv`.
- CI: replay the recorded tool decisions against the same oracles (free, deterministic).

No AER/FYpT ratio is published from this pilot: n = 1 per cell is not a benchmark result.
