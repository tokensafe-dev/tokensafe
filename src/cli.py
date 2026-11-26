#!/usr/bin/env python3
"""
TokenSafe Unified CLI Dispatcher.
Used by:
- npm CLI (`bin/tokensafe.js`)
- standalone executable (`bin/tokensafe`)
- PyPI console script entrypoint (`tokensafe` via `pyproject.toml`)
"""

import sys
import os
import subprocess
from typing import Optional, List, Dict, Any

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.metrics import MetricsTracker
from src.dashboard import Dashboard, render_bar
from src.compactor import PromptCompactor
from src.tokenizer import count_tokens

def summarize_unittest_output(raw: str, full_traceback: bool = False) -> str:
    """Compacts `python3 -m unittest discover` output to what a developer
    actually needs: one green line, or per-failure test name + root cause.
    Keeps the suite run out of the context window (the raw output of a
    300+ test run with tracebacks is thousands of tokens)."""
    lines = raw.splitlines()
    ran = status = ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("Ran ") and " test" in s:
            ran = s
        elif s.startswith("OK") or s.startswith("FAILED"):
            # "OK (skipped=N)" must count as success too, not just bare "OK".
            status = s
    # Failure blocks: one '=' * 70 line PER failure, then the title line,
    # a '-' * 70 rule and the traceback — each block is self-contained.
    blocks, cur = [], None
    for ln in lines:
        if len(ln) >= 10 and set(ln) == {"="}:
            if cur is not None:
                blocks.append(cur)
            cur = []
            continue
        if cur is not None:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)

    failures = []
    for block in blocks:
        title_idx = next((j for j, l in enumerate(block) if l.startswith(("FAIL: ", "ERROR: "))), None)
        if title_idx is None:
            continue
        title = block[title_idx]
        body = [l for l in block[title_idx + 1:]
                if not (len(l) >= 10 and set(l) == {"-"})
                and not l.strip().startswith(("Ran ", "FAILED", "OK"))]
        kind, name = title.split(" ", 1)
        if full_traceback:
            detail = [l.rstrip() for l in body if l.strip()]
        else:
            loc = next((l.strip() for l in reversed(body) if l.strip().startswith('File "')), "")
            cause_lines = [l.strip() for l in reversed(body)
                           if l.strip() and not l.strip().startswith('File "')
                           and not l.strip().startswith(("self.", "assert", "raise"))]
            # assertEqual-style failures end with a diff block ('- a' / '+ b');
            # the actual AssertionError line sits above it.
            cause = next((l for l in cause_lines if not l.startswith(("+", "-", "?", "!"))),
                         cause_lines[0] if cause_lines else "")
            if len(cause) > 200:
                cause = cause[:200] + " …"
            detail = [l for l in (loc, cause) if l]
        failures.append((kind.rstrip(": "), name, detail))

    if not ran and not failures:
        # Crash before unittest could report (import error, bad path):
        # fall back to the tail, which carries the exception.
        tail = [l for l in lines if l.strip()][-15:]
        return "\n".join(tail) if tail else "(no unittest output captured)"

    if status.startswith("OK"):
        return f"\033[32m✓ {ran} — all tests passed\033[0m"

    out = [f"\033[31m✗ {ran} — {status}\033[0m"]
    for kind, name, detail in failures:
        out.append(f"  \033[31m{kind}\033[0m {name}")
        for d in detail:
            out.append(f"      {d}")
    out.append("Full output: python3 -m unittest discover tests  (or: tokensafe tests --traceback)")
    return "\n".join(out)


def _print_help():
    print("TokenSafe CLI — usage: tokensafe <command> [args]\n")
    print("Commands:")
    print("  dev [dir] [--port N] [--host H] [--open]  Start Vite+React DevServer with OUMV (zero repo footprint)")
    print("  integrate [--dry-run]          Retrofit OUMV into existing project (React, Vue, FastAPI, Go)")
    print("  adapters [list]                Catalog of frontend/backend OUMV runtime adapters")
    print("  oumv doctor / status           Diagnose OUMV configuration, ports, and permissions")
    print("  create <name> [--template ...] Greenfield project scaffolder (tokensafe-ui + tokensafe-backend)")
    print("  init [--template ...]          Initialize TokenSafe stack in current directory")
    print("  stats / markdown / html        Savings analytics (terminal / .md report / dashboard)")
    print("  status                         One-screen health check: hooks, weekly savings, tier, sync")
    print("  budget [--transcript=...]      Context Window Budget HUD & Context Quality Score (Grade S-F)")
    print("  compact <transcript.jsonl>     Fold conversation transcript into Consolidated State Ledger")
    print("  transcripts [--recent N]       Analyze agent transcripts (Antigravity/Claude/Codex)")
    print("              [--compare A B]    A/B comparison of two session transcripts")
    print("  benchmark                      Zero-context-loss benchmark suite (simulated SWE-bench harness)")
    print("  bench --real [--repo=key|--here]  Measured benchmark on real repos (zero-loss gates)")
    print("  graph / map / skeleton         Architecture graph / API surface / AST skeleton of a file")
    print("  test <prompt>                  Preview TOCP S-expression prompt compaction")
    print("  guard <command>                Pre-flight safety check for a shell command")
    print("  expand-ref <hash>              Restore folded tool/command/file result by its 8-char hash")
    print("  wrap <command>                 Run command with compact output folding (e.g. tests/logs)")
    print("  web <url>                      Fetch web page/docs and print clean compact Markdown")
    print("  doctor [--fix | --cleanup]     Audit MCP/skill overhead, repair configs & consolidate ghost rules")
    print("  skills [list|disable|enable]   Inspect and toggle active skills to cut turn context tax")
    print("  tests [pytest] [--traceback]   Run the unit test suite (compact failure report)")
    print("  prune [--days N | --max N]     Compact metrics database (roll old events)")
    print("  vacuum                         Reclaim SQLite disk space")
    print("  reset                          Reset all local token metrics")
    print("  install | setup [--all]        Configure detected agent environments")
    print("  uninstall [--all | --project]  Remove TokenSafe from agent environments & unwire proxy")
    print("  update | upgrade               Self-update npm/PyPI package and re-sync agents")
    print("  check-update                   Check for a newer published version")
    print("  activate / license             License activation and status")
    print("  booster on|off|status          Cloud Prompt Booster control (paid tiers)")
    print("  login / auth                   Authorize this device via browser login")
    print("  logout / unlink                Unlink this device and remove local license")
    print("  sync                           Sync telemetry with the cloud backend")
    print("  providers / proxy / mcp        Providers info / proxy server / MCP stdio server")
    print("  --version                      Print version and exit")


def _booster_consent_prompt() -> None:
    """One-time Cloud Prompt Booster consent after a PAID license activation.

    Asks [Y/n] with an honest data-flow disclosure (default Y). Non-TTY runs
    keep the legacy default (on) and print a disable hint instead of blocking.
    Users who already chose (config/env) are never re-asked. Fail-open: a
    broken TTY never blocks activation.
    """
    try:
        from src.distiller import UniversalDistiller
        if UniversalDistiller.is_self_hosted():
            return  # BYOK self-hosted: own keys are their own explicit opt-in
        _enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled()
        if explicitly_set:
            return
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive:
            print("\n  \033[1m⚡ Cloud Prompt Booster\033[0m — distills long prompts (100+ tokens) cloud-side,")
            print("  ~5x deeper compression than the local engine (measured median 71% vs 14%).")
            print("  \033[2mWhat leaves this machine:\033[0m the prompt text (may include code snippets or stack")
            print("  \033[2mtraces) with secrets automatically scrubbed. Processed transiently via TokenSafe")
            print("  \033[2mCloud (Google Gemini Flash-Lite); raw text is never stored and not used for training.\033[0m")
            try:
                answer = input("  Enable Cloud Prompt Booster? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
                print()
            chosen = answer in ("", "y", "yes")
        else:
            chosen = True  # non-interactive: keep the legacy default + hint
        UniversalDistiller.write_config_value("cloud_distill", chosen)
        if chosen:
            suffix = "" if interactive else "  \033[2m(default — run \033[36mtokensafe booster off\033[0m \033[2mto disable)\033[0m"
            print(f"  \033[32m✔ Cloud Prompt Booster enabled.\033[0m{suffix}")
        else:
            print("  \033[33m○ Cloud Prompt Booster off — all prompt processing stays 100% local.\033[0m")
    except Exception:
        pass


def main(argv: Optional[List[str]] = None):
    args = sys.argv[1:] if argv is None else list(argv)
    command = args[0] if args else "stats"

    if command in ("--help", "-h", "help"):
        _print_help()
        return

    # Fast path: version queries must not construct MetricsTracker (which creates
    # the data dir and SQLite database). `tokensafe --version` may run in read-only
    # or foreign directories and must stay side-effect free.
    if command in ("version", "--version", "-v"):
        from src.version_checker import CURRENT_VERSION, VersionChecker
        print(f"TokenSafe v{CURRENT_VERSION}")
        if "--check" in args or "-c" in args:
            banner = VersionChecker.get_banner()
            if banner:
                print(banner)
            else:
                print("\033[32m✔ TokenSafe is up to date.\033[0m")
            if "--notify" in args or "--prompt" in args:
                VersionChecker.notify_os_update(force=True)
        return

    if command in ("status", "health"):
        from src.installer import AgentInstaller
        from src.license_manager import LicenseManager
        from src.telemetry_sync import TelemetrySync
        tracker = MetricsTracker()
        print("\n\033[1m\033[36m⚡ TOKENSAFE STATUS ⚡\033[0m\n")
        # Hooks wiring (best-effort scan of agent config files and plugin dirs)
        active_agents = []
        for key, info in AgentInstaller.detect_available_agents().items():
            hit = any(info.get(pk) and os.path.exists(info[pk]) for pk in
                      ("target_path", "plugin_path", "skills_path", "agents_md_path", "rules_path", "extension_path"))
            cfg = info.get("config_path")
            if not hit and cfg and os.path.exists(cfg):
                try:
                    with open(cfg, "r", encoding="utf-8", errors="replace") as f:
                        hit = "tokensafe" in f.read().lower()
                except Exception:
                    pass
            if hit:
                active_agents.append(key)
        if active_agents:
            print(f"  Hooks:     \033[32m✔ active\033[0m in {len(active_agents)} agent(s): {', '.join(active_agents)}")
        else:
            print("  Hooks:     \033[33m○ not detected\033[0m — run \033[1mtokensafe install\033[0m to configure your agents")
        # Context Quality Health Indicator (S/A/B/C/F)
        try:
            from src.quality_score import ContextQualityScore
            q_score = ContextQualityScore.evaluate(tracker)
            color = "\033[32m" if q_score["grade"] in ("S", "A") else ("\033[33m" if q_score["grade"] == "B" else "\033[31m")
            tax_str = f"~{q_score['static_tax_tokens']:,} tok/turn" if q_score['static_tax_tokens'] else "0 tok/turn"
            print(f"  Context:   {color}\033[1mGrade {q_score['grade']} ({q_score['label']})\033[0m — {q_score['headroom_pct']:.0f}% headroom (static tax: {tax_str})")
        except Exception:
            pass
        # The value moment: recent measured savings
        weekly = tracker.get_weekly_saved()
        print(f"  This Week: \033[32m\033[1m{weekly['saved_tokens']:,} tokens saved\033[0m across {weekly['events']:,} events (≈${weekly['usd_saved']:.2f})")
        # Tier + quota state
        tier, payload = LicenseManager.get_active_license()
        quota = LicenseManager.check_quota(tracker)
        if tier == "guest":
            q_saved = int(quota.get("monthly_saved", 0))
            q_limit = int(quota.get("monthly_limit") or 50_000)
            q_pct = min(100, int(q_saved / q_limit * 100)) if q_limit else 0
            q_state = "\033[31mQUOTA REACHED (Optimizers paused)\033[0m" if quota.get("is_quota_exceeded") else f"{q_pct}% of monthly guest limit"
            print(f"  Tier:      \033[33mGUEST (Offline)\033[0m — {q_saved:,} / {q_limit:,} tokens ({q_state})")
            print("  Unlock 500k: Run \033[36mtokensafe login\033[0m for free Community account & Web Dashboard")
        elif tier == "free":
            q_saved = int(quota.get("monthly_saved", 0))
            q_limit = int(quota.get("monthly_limit") or 500_000)
            q_pct = min(100, int(q_saved / q_limit * 100)) if q_limit else 0
            q_state = "\033[31mQUOTA REACHED — Pro optimizations paused\033[0m" if quota.get("is_quota_exceeded") else f"{q_pct}% of monthly measured quota"
            print(f"  Tier:      \033[33mCOMMUNITY (Free Account)\033[0m — {q_saved:,} / {q_limit:,} tokens ({q_state})")
            print("  Upgrade:   https://tokensafe.dev/signup ($9/mo Solo · 7-Day Team Trial)")
        else:
            print(f"  Tier:      \033[32m{tier.upper()}\033[0m (Unlimited)")
            try:
                from src.distiller import UniversalDistiller
                enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled(tier)
                state = "\033[32mon\033[0m" if enabled else "\033[33moff\033[0m"
                if not explicitly_set:
                    state += " \033[2m(active by default — choose: \033[36mtokensafe booster on|off\033[0m\033[2m)\033[0m"
                print(f"  Booster:   Cloud Prompt Booster {state}")
            except Exception:
                pass
        # Verifiable egress transparency: what left this machine today
        try:
            from src.egress_counter import today_counts
            _eg = today_counts()
            print(f"  Egress:    today — {_eg['booster']} booster · {_eg['telemetry']} telemetry outbound requests")
        except Exception:
            pass
        # Cloud heartbeat (non-blocking; keeps the machine visible server-side)
        try:
            TelemetrySync.sync_background()
            pending = int(TelemetrySync.get_sync_status().get("pending_events", 0) or 0)
            if pending:
                print(f"  Cloud:     {pending:,} events queued for telemetry sync (background)")
            else:
                print("  Cloud:     \033[32m✔ telemetry up to date\033[0m")
        except Exception:
            pass
        # Distillation cache visibility (get_stats existed but was never surfaced anywhere)
        try:
            from src.distiller_cache import DistillationCache
            cstats = DistillationCache.get_stats()
            lookups = cstats["hits"] + cstats["misses"]
            if lookups:
                print(f"  Cache:     \033[32m{cstats['hit_ratio_pct']}% distillation cache hit ratio\033[0m ({cstats['hits']:,}/{lookups:,} lookups · {cstats['persistent_entries']:,} entries on disk)")
        except Exception:
            pass
        print()

    elif command in ("budget", "quality", "score"):
        from src.governor import ContextBudget
        from src.quality_score import ContextQualityScore
        from src.environment_auditor import EnvironmentAuditor
        tracker = MetricsTracker()
        model = args[1] if len(args) > 1 and not args[1].startswith("-") else ""
        transcript = ""
        for a in args[1:]:
            if a.startswith("--transcript="):
                transcript = a.split("=", 1)[1]
            elif a.endswith(".jsonl"):
                transcript = a
        budget = ContextBudget.estimate(tracker, session_id="mcp_session", model=model, transcript_path=transcript)
        audit = EnvironmentAuditor.audit()
        q_score = ContextQualityScore.calculate(budget=budget, audit=audit)

        color = "\033[32m" if q_score["grade"] in ("S", "A") else ("\033[33m" if q_score["grade"] == "B" else "\033[31m")
        print("\n\033[1m\033[36m⚡ TOKENSAFE CONTEXT QUALITY & BUDGET HUD ⚡\033[0m\n")
        print(f"  Health Grade:    {color}\033[1mGrade {q_score['grade']} ({q_score['label']})\033[0m — {q_score['score']}/100 pts")
        print(f"  Window Headroom: {color}{q_score['headroom_pct']:.1f}% free\033[0m ({budget['used_estimated']:,} / {budget['window_tokens']:,} tokens estimated)")
        print(f"  Static Tax:      ~{q_score['static_tax_tokens']:,} tokens on every API turn")
        print(f"  Breakdown:       tools: {budget['breakdown']['tools']:,} | history: {budget['breakdown']['history']:,} | prompts: {budget['breakdown']['prompts']:,}")
        if q_score["deductions"]:
            print(f"  Deductions:      {', '.join(q_score['deductions'])}")
        print(f"  Recommendation:  {budget['recommendation']}\n")

    elif command in ("compact", "fold"):
        if len(args) < 2:
            print("Usage: tokensafe compact <path_to_transcript.jsonl>")
            sys.exit(1)
        transcript_path = args[1].strip()
        from src.history_compactor import HistoryCompactor
        ledger, orig_t, fold_t, stats = HistoryCompactor.fold_transcript(transcript_path, invocation_num=3)
        if not ledger:
            print("\n\033[33m○ Transcript not compactable (requires >= 4 steps and >= 1,000 history tokens).\033[0m\n")
            sys.exit(0)
        print("\n\033[1m\033[36m⚡ TOKENSAFE COMPACT CONVERSATION ⚡\033[0m\n")
        print(f"  Original history: {orig_t:,} tokens")
        print(f"  Compacted ledger: {fold_t:,} tokens")
        print(f"  Tokens saved:     \033[32m\033[1m+{orig_t - fold_t:,} tokens\033[0m\n")
        print(ledger)
        print()

    elif command in ("stats", "dashboard"):
        tracker = MetricsTracker()
        dashboard = Dashboard(tracker)
        try:
            from src.telemetry_sync import TelemetrySync
            TelemetrySync.sync_background()
        except Exception:
            pass
        print(dashboard.render_terminal())

    elif command in ("html", "dashboard-html"):
        from src.paths import resolve_data_dir
        tracker = MetricsTracker()
        dashboard = Dashboard(tracker)
        html_content = dashboard.render_html()
        out_path = os.path.join(resolve_data_dir(), "dashboard.html")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"\n⚡ Interactive HTML Dashboard generated at: {out_path}")
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", out_path], check=False)
            elif os.name == "nt":
                os.startfile(out_path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", out_path], check=False)
            print("   Opened in default browser.")
        except Exception:
            try:
                import webbrowser
                webbrowser.open(f"file://{os.path.abspath(out_path)}")
            except Exception:
                pass

    elif command in ("benchmark", "bench"):
        if "--real" in args:
            # Measured real-repo mode: pre-registered scenarios (src/bench_scenarios.json),
            # zero-loss gates, real clones — see src/benchmark_real.py methodology header.
            # --here: measure the CURRENT repository instead (god-node hot symbols,
            # suite as-is without mutation — self-measurement, not the published set).
            from src.benchmark_real import RealRepoBenchmark
            bench = RealRepoBenchmark()
            if "--here" in args:
                print("\n⚡ Running REAL-REPO benchmark on the CURRENT repository (measured, zero-loss gates)...")
                data = bench.run_local(os.getcwd())
            else:
                repo_keys = [a.split("=", 1)[1] for a in args if a.startswith("--repo=")]
                print("\n⚡ Running REAL-REPO benchmark (measured on live clones, pre-registered scenarios)...")
                data = bench.run_all(repo_keys or None)
            print()
            print(bench.render_markdown(data))
            out = bench.export(data)
            print(f"📊 JSON: {out['json']}")
            print(f"📊 MD:   {out['md']}\n")
            sys.exit(0 if all(
                (s.get("status") in ("ok", "skipped"))
                for r in data["results"] for s in (r.get("symbol_read"), r.get("test_output"), r.get("multiturn"))
                if s
            ) else 1)
        from src.benchmark import BenchmarkSuite
        from src.paths import resolve_data_dir
        print("\n⚡ Running Zero-Loss SWE-bench Benchmark Suite across real-world scenarios...")
        data = BenchmarkSuite.run_all()
        print(BenchmarkSuite.render_report(data))
        out_md = os.path.join(resolve_data_dir(), "BENCHMARK_REPORT.md")
        BenchmarkSuite.export_markdown_report(data, out_md)
        print(f"📊 Publication-ready markdown report saved to: {out_md}\n")

    elif command in ("setup", "install"):
        from src.installer import AgentInstaller
        if "--all" in args or "-a" in args or "-y" in args or "--auto" in args:
            print("\n\033[1m\033[36m⚡ TOKENSAFE UNIVERSAL MULTI-AGENT INSTALLER (--all) ⚡\033[0m\n")
            res = AgentInstaller.install_all(force=True)
            print("Configured AI agent environments:")
            for k, r in res["agents"].items():
                status = "\033[32m✔\033[0m" if r["success"] else "\033[31m✖\033[0m"
                print(f"  {status} \033[1m{r['name']:<28}\033[0m: {r['message']}")
            print(f"\n\033[32m✔ Universal project rules injected into: {', '.join(res['project_rules'])}\033[0m\n")
            # Proxy default-on is asserted INSIDE install_all (_ensure_proxy_default);
            # the retired interactive prompt must never be referenced here again.
        elif "--project" in args:
            agents_plugin = None
            if "--agents-plugin" in args:
                idx = args.index("--agents-plugin")
                if idx + 1 < len(args):
                    agents_plugin = args[idx + 1]
            res = AgentInstaller.install_project(os.getcwd(), agents_plugin_path=agents_plugin)
            print(f"\n\033[32m✔ TokenSafe rules injected into project: {', '.join(res['created_rules'])}\033[0m\n")
        elif "--refresh" in args or "--sync-installed" in args:
            res = AgentInstaller.refresh_installed_agents()
            print(f"\n\033[32m✔ Synchronized {res['synced_count']} installed/configured agent environment(s).\033[0m\n")
        else:
            AgentInstaller.run_interactive()

    elif command in ("uninstall", "unistall", "remove", "unlink-all"):
        from src.installer import AgentInstaller
        purge = "--purge" in args
        if "--all" in args or "-a" in args or "-y" in args or "--auto" in args:
            print("\n\033[1m\033[36m⚡ TOKENSAFE UNIVERSAL MULTI-AGENT UNINSTALLER (--all) ⚡\033[0m\n")
            res = AgentInstaller.uninstall_all(force=True, purge=purge)
            print("Uninstalled AI agent configurations:")
            for k, r in res["agents"].items():
                status = "\033[32m✔\033[0m" if r["success"] else "\033[33m–\033[0m"
                print(f"  {status} \033[1m{r['name']:<28}\033[0m: {r['message']}")
            if res.get("project_rules"):
                print(f"\n\033[32m✔ Project rules cleaned from: {', '.join(res['project_rules'])}\033[0m")
            if res.get("proxy"):
                print(f"  \033[32m✔\033[0m Proxy daemon stopped and routing restored")
            if purge:
                print(f"  \033[32m✔\033[0m Local data directory purged (~/.tokensafe)")
            print("\n\033[32m✔ TokenSafe uninstallation complete.\033[0m\n")
        elif "--project" in args:
            res = AgentInstaller.uninstall_project(os.getcwd())
            cleaned = res.get("cleaned_rules", [])
            msg = f"TokenSafe rules cleaned from project: {', '.join(cleaned)}" if cleaned else "No TokenSafe rules found in project workspace"
            print(f"\n\033[32m✔ {msg}\033[0m\n")
        else:
            AgentInstaller.run_interactive_uninstall()

    elif command in ("update", "upgrade"):
        from src.installer import AgentInstaller
        AgentInstaller.self_update()

    elif command in ("check-update", "check_update"):
        from src.version_checker import CURRENT_VERSION, VersionChecker
        print(f"\n\033[1m\033[36m⚡ CHECKING FOR TOKENSAFE UPDATES ⚡\033[0m\n")
        print(f"  Current version: v{CURRENT_VERSION}")
        info = VersionChecker.check_version(force_fetch=True)
        latest = info.get("latest_version", CURRENT_VERSION)
        avail = info.get("update_available", False)
        print(f"  Latest version:  v{latest}")
        try:
            from src.license_manager import LicenseManager
            tier, lic = LicenseManager.get_active_license()
            if tier == "guest":
                # NOTE: no function-local MetricsTracker import here — it would
                # shadow the module-level import (L20) for the WHOLE main()
                # and crash `tokensafe status` with UnboundLocalError.
                quota = LicenseManager.check_quota(MetricsTracker())
                saved = int(quota.get("monthly_saved", 0))
                pct = min(100.0, (saved / 50_000) * 100) if 50_000 > 0 else 0.0
                print(f"  \033[2mQuota:\033[0m           \033[33mGuest Mode\033[0m ({saved:,} / 50,000 tokens used, {pct:.1f}%) • Run '\033[36mtokensafe login\033[0m' for 500k/mo.")
        except Exception:
            pass
        banner = VersionChecker.get_banner()
        if banner:
            print(banner)
        else:
            print("\n  \033[32m✔ TokenSafe is up to date.\033[0m\n")
        if avail or "--notify" in args or "--prompt" in args or "-p" in args:
            print("  \033[36mTriggering native OS update dialog...\033[0m")
            VersionChecker.notify_os_update(force=True)

    elif command in ("guard", "security"):
        from src.guard import TokenSafeGuard
        sub = args[1] if len(args) > 1 else "help"
        if sub in ("audit", "scan"):
            target_path = args[2] if len(args) > 2 else os.getcwd()
            if not os.path.exists(target_path):
                print(f"\n\033[31m✖ Target path not found: {target_path}\033[0m\n")
                sys.exit(1)
            print(f"\n\033[1m\033[36m🛡️ TOKENSAFE SECURITY & HYGIENE AUDIT 🛡️\033[0m")
            print(f"  Target: {target_path}\n")
            violations = []
            files_scanned = 0
            code_exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java")
            if os.path.isfile(target_path):
                files_scanned = 1
                try:
                    with open(target_path, "r", encoding="utf-8", errors="replace") as fp:
                        content = fp.read()
                    res = TokenSafeGuard.inspect_file_write(target_path, content)
                    if res.blocked:
                        violations.append((os.path.basename(target_path), res.check_name, res.reason))
                except Exception:
                    pass
            else:
                for root, dirs, files in os.walk(target_path):
                    dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "dist", "build", ".next", ".venv", "venv", "__pycache__")]
                    for f in files:
                        if f.endswith(code_exts):
                            files_scanned += 1
                            full_p = os.path.join(root, f)
                            try:
                                with open(full_p, "r", encoding="utf-8", errors="replace") as fp:
                                    content = fp.read()
                                res = TokenSafeGuard.inspect_file_write(full_p, content)
                                if res.blocked:
                                    rel_p = os.path.relpath(full_p, target_path)
                                    violations.append((rel_p, res.check_name, res.reason))
                            except Exception:
                                pass
            if not violations:
                print(f"  \033[32m✔ All {files_scanned} files passed TokenSafe Guard audit cleanly! Zero secret fallbacks or SQLi.\033[0m\n")
            else:
                print(f"  \033[31m✖ Found {len(violations)} security violations in {files_scanned} files:\033[0m\n")
                for rel_p, check, reason in violations:
                    print(f"    \033[31m• [{check}]\033[0m {rel_p}: {reason}")
                print()
        elif sub in ("check", "test") and len(args) > 2:
            cmd_to_test = " ".join(args[2:])
            print(f"\n\033[1m\033[36m🛡️ TOKENSAFE COMMAND GUARD CHECK 🛡️\033[0m\n")
            print(f"  Command: \033[1m{cmd_to_test}\033[0m")
            res = TokenSafeGuard.inspect_command(cmd_to_test)
            if res.blocked:
                print(f"  Status:  \033[31m\033[1mBLOCKED\033[0m [{res.check_name}]")
                print(f"  Reason:  {res.reason}\n")
            elif res.warning_only:
                print(f"  Status:  \033[33m\033[1mWARNING\033[0m [{res.check_name}]")
                print(f"  Notice:  {res.reason}\n")
            else:
                print(f"  Status:  \033[32m\033[1mALLOWED\033[0m (Command is safe and policy-compliant)\n")
        elif sub not in ("help", "--help", "-h", "check", "test") and len(args) > 1:
            cmd_to_test = " ".join(args[1:])
            print(f"\n\033[1m\033[36m🛡️ TOKENSAFE COMMAND GUARD CHECK 🛡️\033[0m\n")
            print(f"  Command: \033[1m{cmd_to_test}\033[0m")
            res = TokenSafeGuard.inspect_command(cmd_to_test)
            if res.blocked:
                print(f"  Status:  \033[31m\033[1mBLOCKED\033[0m [{res.check_name}]")
                print(f"  Reason:  {res.reason}\n")
            elif res.warning_only:
                print(f"  Status:  \033[33m\033[1mWARNING\033[0m [{res.check_name}]")
                print(f"  Notice:  {res.reason}\n")
            else:
                print(f"  Status:  \033[32m\033[1mALLOWED\033[0m (Command is safe and policy-compliant)\n")
        else:
            print("\nUsage:")
            print("  tokensafe guard \"<command>\"       Audit a terminal command line")
            print("  tokensafe guard check \"<command>\" Audit a terminal command line")
            print("  tokensafe guard audit [path]      Recursively audit source files for secrets & vulnerabilities\n")

    elif command in ("create", "new"):
        from src.scaffold import ProjectScaffolder
        project_name = None
        template = "fullstack"
        force = False

        i = 1
        while i < len(args):
            arg = args[i]
            if arg in ("--help", "-h"):
                print("\n⚡ TokenSafe Project Scaffolder\n")
                print("Usage:")
                print("  tokensafe create <project-name> [options]\n")
                print("Options:")
                print("  --template <type>   fullstack (default), ui, backend, python, go")
                print("  --force, -f         Overwrite existing files")
                print("  --help, -h          Show this help screen\n")
                print("Examples:")
                print("  tokensafe create my-app")
                print("  tokensafe create my-dashboard --template ui")
                print("  tokensafe create store-api --template backend\n")
                return
            elif arg in ("--template", "-t") and i + 1 < len(args):
                template = args[i + 1]
                i += 2
                continue
            elif arg.startswith("--template="):
                template = arg.split("=", 1)[1]
            elif arg in ("--force", "-f"):
                force = True
            elif not arg.startswith("-") and project_name is None:
                project_name = arg
            i += 1

        if not project_name:
            print("\n❌ Error: Project name is required.")
            print("Usage: tokensafe create <project-name> [--template fullstack|ui|backend]\n")
            return

        target_dir = os.path.abspath(project_name)
        try:
            res = ProjectScaffolder.scaffold(
                target_dir=target_dir,
                project_name=project_name,
                template=template,
                force=force,
            )
            print(f"\n\033[1m\033[36m⚡ TOKENSAFE GREENFIELD SCAFFOLDER ⚡\033[0m\n")
            print(f"  ✔ Created project: \033[1m{res['project_name']}\033[0m")
            print(f"  ✔ Template:       \033[32m{res['template']}\033[0m")
            print(f"  ✔ Location:       {res['target_dir']}")
            print(f"  ✔ Files created:  {len(res['created_files'])}\n")
            print("Next steps:")
            print(f"  cd {project_name}")
            if template in ("fullstack", "ui"):
                sub = "frontend" if template == "fullstack" else "."
                print(f"  cd {sub} && npm install && npm run dev")
            if template in ("fullstack", "backend", "python", "fastapi"):
                sub = "backend" if template == "fullstack" else "."
                print(f"  cd {sub} && pip install -e . && python main.py")
            print("\n  OUMV runtime and agent directives are pre-configured out-of-the-box.\n")
        except Exception as e:
            print(f"\n❌ Scaffolding failed: {e}\n")

    elif command == "init":
        from src.scaffold import ProjectScaffolder
        template = "fullstack"
        force = False

        i = 1
        while i < len(args):
            arg = args[i]
            if arg in ("--help", "-h"):
                print("\n⚡ TokenSafe Project Initializer\n")
                print("Usage:")
                print("  tokensafe init [options]\n")
                print("Options:")
                print("  --template <type>   fullstack (default), ui, backend, python, go")
                print("  --force, -f         Overwrite existing files\n")
                return
            elif arg in ("--template", "-t") and i + 1 < len(args):
                template = args[i + 1]
                i += 2
                continue
            elif arg.startswith("--template="):
                template = arg.split("=", 1)[1]
            elif arg in ("--force", "-f"):
                force = True
            i += 1

        cwd = os.getcwd()
        project_name = os.path.basename(cwd)
        try:
            res = ProjectScaffolder.scaffold(
                target_dir=cwd,
                project_name=project_name,
                template=template,
                force=force,
            )
            print(f"\n\033[1m\033[36m⚡ TOKENSAFE PROJECT INITIALIZED ⚡\033[0m\n")
            print(f"  ✔ Initialized:    \033[1m{res['project_name']}\033[0m")
            print(f"  ✔ Template:       \033[32m{res['template']}\033[0m")
            print(f"  ✔ Files created:  {len(res['created_files'])}\n")
        except Exception as e:
            print(f"\n❌ Initialization failed: {e}\n")

    elif command in ("integrate", "retrofit"):
        import json
        from src.retrofit import detect_project_stack, generate_config
        dry_run = "--dry-run" in args
        force = "--force" in args or "-f" in args
        cwd = os.getcwd()

        print(f"\n\033[1m\033[36m⚡ TOKENSAFE OUMV RETROFIT INTEGRATION ⚡\033[0m\n")
        stack = detect_project_stack(cwd)
        fe = stack.get("frontend")
        be = stack.get("backend")

        print(f"  Workspace:  {cwd}")
        print(f"  Frontend:   \033[32m{fe['framework']} ({fe['provider']})\033[0m" if fe else "  Frontend:   \033[33mNone detected\033[0m")
        print(f"  Backend:    \033[32m{be['framework']} ({be['provider']})\033[0m" if be else "  Backend:    \033[33mNone detected\033[0m")
        print(f"  Manifests:  {', '.join(stack.get('detected_files', [])) or 'None'}\n")

        cfg, conf_file, created = generate_config(cwd, stack, dry_run=dry_run, force=force)
        if dry_run:
            print(f"  \033[33m[DRY-RUN]\033[0m Proposed configuration (.tokensafe/oumv.json):")
            print(json.dumps(cfg, indent=4))
        elif created:
            mutate_enabled = cfg.get("permissions", {}).get("mutate", True)
            policy = (
                "\033[32menabled out-of-the-box (OCC + syntax guard + backups)\033[0m"
                if mutate_enabled
                else "\033[33mread-only (mutate: false)\033[0m"
            )
            print(f"  ✔ Generated OUMV config: \033[1m{conf_file}\033[0m")
            print(f"  ✔ Mutation policy:       {policy}")
            print(f"  ✔ Profiles enabled:      {', '.join(cfg.get('oumv', {}).get('profiles', []))}")
            print("\n  Project is now retrofitted for OUMV. AI coding agents can inspect components and endpoints.\n")
        else:
            print(f"  ✔ Existing configuration preserved: \033[1m{conf_file}\033[0m (use --force to overwrite)\n")

    elif command in ("adapters", "adapter"):
        from src.retrofit import get_adapters_catalog
        catalog = get_adapters_catalog()
        print(f"\n\033[1m\033[36m⚡ TOKENSAFE OUMV ADAPTERS CATALOG ⚡\033[0m\n")
        print(f"  {'ADAPTER ID':<20} {'PROFILE':<10} {'REPO':<26} {'STATUS':<10}")
        print("  " + "-" * 70)
        for a in catalog:
            color = "\033[32m" if a["status"] == "ready" else "\033[33m"
            print(f"  {a['id']:<20} {a['profile']:<10} {a['repo']:<26} {color}{a['status']:<10}\033[0m")
        print("\n  Run \033[1mtokensafe integrate\033[0m in any project to auto-detect and configure compatible adapters.\n")

    elif command == "oumv":
        sub = args[1] if len(args) > 1 else "status"
        if sub in ("doctor", "audit"):
            from src.retrofit import run_doctor
            doc = run_doctor(os.getcwd())
            print(f"\n\033[1m\033[36m⚡ TOKENSAFE OUMV DOCTOR ⚡\033[0m\n")
            print(f"  Config (.tokensafe/oumv.json):     {'✔ Present' if doc['config_present'] else '○ Not found (run tokensafe integrate)'}")
            print(f"  Runtime Beacon (.tokensafe/...):   {'✔ Active' if doc['runtime_beacon_present'] else '○ Offline (DevServer not running)'}")
            fe = doc['detected_stack'].get('frontend')
            be = doc['detected_stack'].get('backend')
            print(f"  Detected Frontend:                 {fe['framework'] if fe else 'None'}")
            print(f"  Detected Backend:                  {be['framework'] if be else 'None'}")
            mut = doc['permissions'].get('mutate', False)
            print(f"  Mutation Permission:              {'✔ Enabled' if mut else '○ Read-Only (Default safe)'}\n")
        else:
            from src.oumv_gateway import OUMVGateway
            status = OUMVGateway.status()
            print(f"\n\033[1m\033[36m⚡ TOKENSAFE OUMV RUNTIME STATUS ⚡\033[0m\n")
            print(f"  Frontend Runtime:  {status['frontend'].get('status', 'OK')} ({status['frontend'].get('name', 'N/A')})")
            print(f"  Backend Runtime:   {status['backend'].get('status', 'OK')} ({status['backend'].get('name', 'N/A')})")
            print(f"  Workspace:         {status.get('workspace')}\n")

    elif command == "dev":
        from src.retrofit import resolve_zero_footprint_vite_command
        target_dir = os.getcwd()
        port_val: Optional[int] = None
        host_val: Optional[str] = None
        open_browser = False
        unknown_flags: List[str] = []
        i = 1
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a.startswith("--port="):
                try:
                    port_val = int(a.split("=", 1)[1])
                except ValueError:
                    pass
            elif a == "--port":
                if nxt is not None:
                    try:
                        port_val = int(nxt)
                        i += 1
                    except ValueError:
                        unknown_flags.append(a)
                else:
                    unknown_flags.append(a)
            elif a.startswith("--host="):
                host_val = a.split("=", 1)[1]
            elif a == "--host":
                if nxt is not None and not nxt.startswith("-"):
                    host_val = nxt
                    i += 1
                else:
                    host_val = "true"
            elif a == "--open":
                open_browser = True
            elif a.startswith("-"):
                unknown_flags.append(a)
            else:
                target_dir = os.path.abspath(a)
            i += 1
        if unknown_flags:
            print(f"⚠ Ignoring unsupported 'tokensafe dev' flag(s): {' '.join(unknown_flags)}")
        cmd, source = resolve_zero_footprint_vite_command(
            target_dir, port=port_val, host=host_val, open_browser=open_browser
        )
        print(f"⚡ Launching Zero-Footprint Vite+React OUMV server ({source})...")
        proc = subprocess.run(cmd, cwd=target_dir)
        sys.exit(proc.returncode)

    elif command in ("burn", "burn-monitor"):
        from src.burn_monitor import collect_live_burn_snapshot, evaluate_burn_warnings, is_zai_peak_hour
        from src.transcript_analyzer import discover_transcript_paths, analyze_transcript, render_transcripts_report
        agent_filter = "auto"
        for i, a in enumerate(args):
            if a.startswith("--agent="):
                agent_filter = a.split("=", 1)[1]
            elif a in ("--agent", "-a") and i + 1 < len(args):
                agent_filter = args[i + 1]
        print("\n\033[1m\033[36m🔥 TOKENSAFE MULTI-AGENT LIVE BURN MONITOR 🔥\033[0m\n")
        print(f"  Z.ai Peak Hours (Mon-Fri 14:00-18:00 SST): {'ACTIVE (1.0x = 2x off-peak)' if is_zai_peak_hour() else 'OFF-PEAK (0.5x rate)'}")
        found = discover_transcript_paths(agent=agent_filter, limit=8)
        if not found:
            print("  No active session transcripts/rollouts found.\n")
            sys.exit(0)
        analyzed = [analyze_transcript(fp, agent_type=ag_type) for ag_type, fp, _ in found]
        print(render_transcripts_report(analyzed))
        for ag in ("zcode", "claude_code", "antigravity"):
            if agent_filter not in ("auto", ag):
                continue
            snap = collect_live_burn_snapshot(agent=ag)
            if snap:
                warns = evaluate_burn_warnings(snap)
                if warns:
                    print(f"  [{ag}] Active session alerts:")
                    for w in warns:
                        print(f"    • {w['message']}")
        sys.exit(0)

    elif command in ("transcripts", "transcript"):
        from src.transcript_analyzer import (
            discover_transcript_paths,
            analyze_transcript,
            compare_transcripts,
            render_transcripts_report,
            render_comparison_report
        )
        if "--compare" in args:
            c_idx = args.index("--compare")
            if len(args) < c_idx + 3:
                print("Usage: tokensafe transcripts --compare <before.jsonl> <after.jsonl>")
                sys.exit(1)
            before_f = args[c_idx + 1]
            after_f = args[c_idx + 2]
            res = compare_transcripts(before_f, after_f)
            print(render_comparison_report(res))
            sys.exit(0)

        agent_type = "auto"
        explicit_path = None
        limit = 10
        for i, a in enumerate(args):
            if a in ("--recent", "-n") and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    pass
            elif a.startswith("--agent="):
                agent_type = a.split("=", 1)[1]
            elif a in ("--agent", "-a") and i + 1 < len(args):
                agent_type = args[i + 1]
            elif a.startswith("--path="):
                explicit_path = a.split("=", 1)[1]
            elif a in ("--path", "-p") and i + 1 < len(args):
                explicit_path = args[i + 1]

        found = discover_transcript_paths(agent=agent_type, explicit_path=explicit_path, limit=limit)
        if not found:
            print("No transcripts found. Specify path via --path <path_or_dir> or --agent <antigravity|claude|codex>.")
            sys.exit(0)

        analyzed = []
        for ag_type, fp, _ in found:
            analyzed.append(analyze_transcript(fp, agent_type=ag_type))

        print(render_transcripts_report(analyzed))
        sys.exit(0)

    elif command == "proxy":
        from src.proxy import run_proxy
        sub = args[1] if len(args) > 1 else ""
        if sub in ("--help", "-h", "help"):
            print("Usage: tokensafe proxy [port]        Run in foreground (default: 8765)")
            print("       tokensafe proxy on            Install daemon + wire agents (one command)")
            print("       tokensafe proxy off           Unwire agents + remove daemon (rollback)")
            print("       tokensafe proxy status        Health, wiring state, heartbeat")
            return
        if sub == "on":
            from src.installer import AgentInstaller
            report = AgentInstaller.proxy_on()
            d, w = report["daemon"], report["wiring"]
            print(f"\n\033[1m\033[36m⚡ TOKENSAFE PROXY SETUP ⚡\033[0m\n")
            print(f"  Daemon:    {'✔ ' + d['message'] if d['ok'] else '✖ ' + d['message']}")
            print(f"  Health:    {'✔ proxy answering on 127.0.0.1:8765' if report['health'] else '✖ not reachable'}")
            print(f"  Wiring:    {'✔ ' + w['message'] if w['ok'] else '✖ ' + w['message']}")
            if report["manual_steps"]:
                print("  Manual (not auto-wirable):")
                for step in report["manual_steps"]:
                    print(f"    • {step}")
            print(f"\n  Rollback anytime: \033[1mtokensafe proxy off\033[0m\n")
            return
        if sub == "off":
            from src.installer import AgentInstaller
            report = AgentInstaller.proxy_off()
            u, d = report["unwired"], report["daemon"]
            print(f"\n  Unwired:   {'✔ ' + u['message'] if u['ok'] else '✖ ' + u['message']}")
            print(f"  Daemon:    {'✔ ' + d['message'] if d['ok'] else '✖ ' + d['message']}\n")
            return
        if sub == "status":
            from src.installer import AgentInstaller
            st = AgentInstaller.proxy_status()
            print(f"\n  Health:    {'✔ running on 127.0.0.1:8765' if st['health'] else '✖ not running'}")
            print(f"  Claude Code wired: {'✔ ' + str(st['claude_code_env_value']) if st['claude_code_wired'] else '○ not wired'}")
            hb = st.get("heartbeat_age_seconds")
            print(f"  Last traffic: {f'{hb}s ago' if hb is not None else 'no heartbeat yet'}\n")
            return
        port = 8765
        if len(args) > 1:
            try:
                port = int(args[1])
                if not (1 <= port <= 65535):
                    raise ValueError("port out of range")
            except ValueError:
                print(f"Invalid port '{args[1]}'. Expected integer 1..65535 (default: 8765).")
                sys.exit(1)
        run_proxy(port)

    elif command == "mcp":
        subcmd = args[1] if len(args) > 1 else "serve"
        if subcmd in ("disable", "off", "rm"):
            if len(args) < 3:
                print("Usage: tokensafe mcp disable <server_name>")
                sys.exit(1)
            target = args[2].strip()
            from src.environment_auditor import EnvironmentAuditor
            res = EnvironmentAuditor.disable_mcp(target)
            if res["success"]:
                print(f"\n\033[32m✔ MCP server '{target}' disabled successfully. Removed from active turn overhead.\033[0m\n")
            else:
                print(f"\n\033[31m✖ Server '{target}' not found or already disabled.\033[0m\n")
        elif subcmd in ("enable", "on", "add"):
            if len(args) < 3:
                print("Usage: tokensafe mcp enable <server_name>")
                sys.exit(1)
            target = args[2].strip()
            from src.environment_auditor import EnvironmentAuditor
            res = EnvironmentAuditor.enable_mcp(target)
            if res["success"]:
                print(f"\n\033[32m✔ MCP server '{target}' re-enabled successfully.\033[0m\n")
            else:
                print(f"\n\033[31m✖ Server '{target}' not found in disabled servers.\033[0m\n")
        elif subcmd in ("wrap", "proxy-wrap"):
            from src.mcp_config_manager import MCPConfigManager
            target = args[2].strip() if len(args) > 2 else "--all"
            if target in ("--all", "-a", "all"):
                count = MCPConfigManager.wrap_all()
                print(f"\n\033[32m✔ Wrapped {count} third-party MCP server(s) behind tokensafe mcp-proxy.\033[0m\n")
            else:
                servers = MCPConfigManager.list_servers()
                matched = [s for s in servers if s["server_name"] == target]
                if not matched:
                    print(f"\n\033[31m✖ Server '{target}' not found in active agent MCP configs.\033[0m\n")
                else:
                    for s in matched:
                        MCPConfigManager.wrap_server(s["config_path"], s["server_name"])
                    print(f"\n\033[32m✔ Server '{target}' wrapped behind tokensafe mcp-proxy.\033[0m\n")
        elif subcmd in ("unwrap", "restore"):
            from src.mcp_config_manager import MCPConfigManager
            target = args[2].strip() if len(args) > 2 else "--all"
            if target in ("--all", "-a", "all"):
                count = MCPConfigManager.unwrap_all()
                print(f"\n\033[32m✔ Unwrapped {count} MCP server(s) back to direct execution.\033[0m\n")
            else:
                servers = MCPConfigManager.list_servers()
                matched = [s for s in servers if s["server_name"] == target]
                if not matched:
                    print(f"\n\033[31m✖ Server '{target}' not found in active agent MCP configs.\033[0m\n")
                else:
                    for s in matched:
                        MCPConfigManager.unwrap_server(s["config_path"], s["server_name"])
                    print(f"\n\033[32m✔ Server '{target}' restored to original command.\033[0m\n")
        elif subcmd in ("list", "ls", "status"):
            from src.environment_auditor import EnvironmentAuditor
            print(EnvironmentAuditor.format_cli_report())
        else:
            from src.mcp_server import run_mcp_server
            run_mcp_server()

    elif command in ("mcp-proxy", "mcp_proxy"):
        target_args = list(args[1:])
        if target_args and target_args[0] == "--":
            target_args.pop(0)
        from src.mcp_proxy import run_proxy
        run_proxy(target_args)

    elif command in ("doctor", "audit", "tax"):
        from src.environment_auditor import EnvironmentAuditor
        from src.competitors import CompetitorAuditor

        if "--fix" in args:
            print("\n\033[1m\033[36m🔧 TOKENSAFE ENVIRONMENT AUTO-FIX 🔧\033[0m\n")
            fix_res = EnvironmentAuditor.auto_fix()
            for item in fix_res.get("fixed", []):
                print(f"  \033[32m✔\033[0m {item}")
            for item in fix_res.get("skipped", []):
                print(f"  \033[33m⚠\033[0m {item}")
            if not fix_res.get("fixed") and not fix_res.get("skipped"):
                print("  \033[32m✔\033[0m No auto-fixable config or ghost-token issues found.")
            print()

        print(EnvironmentAuditor.format_cli_report())

        conflicts = CompetitorAuditor.audit()
        if conflicts:
            print("\n\033[1m\033[33m⚡ DETECTED CONFLICTING OR STALE TOOLS ⚡\033[0m")
            for c in conflicts:
                print(f"  • \033[1m{c['name']}\033[0m [{c['class']}]: {c['description']}")
                for tr in c['traces']:
                    loc = tr.get('path') or tr.get('file') or tr.get('config')
                    print(f"    - {tr['kind']}: {loc}")
            print("\n  💡 Tip: Run 'tokensafe doctor --cleanup' (or '--fix') to remove conflicting orphan hooks/rules.\n")

            if "--cleanup" in args:
                for c in conflicts:
                    res = CompetitorAuditor.cleanup(c["id"])
                    print(f"  \033[32m✔ Cleaned {c['name']}: {', '.join(res.get('removed', []))}\033[0m")

    elif command in ("skills", "skill"):
        from src.environment_auditor import EnvironmentAuditor
        sub = args[1] if len(args) > 1 else "list"
        if sub in ("disable", "off", "rm"):
            if len(args) < 3:
                print("Usage: tokensafe skills disable <skill_name>")
                sys.exit(1)
            target = args[2].strip()
            res = EnvironmentAuditor.disable_skill(target)
            if res.get("success"):
                print(f"\n\033[32m✔ Skill '{res['skill']}' disabled ({res['path']}).\033[0m\n")
            else:
                print(f"\n\033[31m✖ {res.get('error', 'Failed to disable skill')}\033[0m\n")
                sys.exit(1)
        elif sub in ("enable", "on", "add"):
            if len(args) < 3:
                print("Usage: tokensafe skills enable <skill_name>")
                sys.exit(1)
            target = args[2].strip()
            res = EnvironmentAuditor.enable_skill(target)
            if res.get("success"):
                print(f"\n\033[32m✔ Skill '{res['skill']}' re-enabled ({res['path']}).\033[0m\n")
            else:
                print(f"\n\033[31m✖ {res.get('error', 'Failed to enable skill')}\033[0m\n")
                sys.exit(1)
        else:
            print(EnvironmentAuditor.format_skills_report())

    elif command in ("web", "fetch", "url"):
        if len(args) < 2:
            print("Usage: tokensafe web <url>")
            sys.exit(1)
        url = args[1].strip()
        from src.web_reader import WebReader
        md, ok, err = WebReader.fetch_url(url)
        if not ok:
            print(f"\033[31mError: {err}\033[0m")
            sys.exit(1)
        print(md)

    elif command in ("search", "find", "fts"):
        if len(args) < 2:
            print("Usage: tokensafe search <query> [--limit 25] [--path <dir>]")
            sys.exit(1)
        query = args[1]
        limit = 25
        target_dir = os.getcwd()
        if "--limit" in args:
            idx = args.index("--limit")
            if idx + 1 < len(args):
                try:
                    limit = int(args[idx + 1])
                except ValueError:
                    pass
        if "--path" in args:
            idx = args.index("--path")
            if idx + 1 < len(args):
                target_dir = os.path.abspath(args[idx + 1])

        from src.fts_index import CodeFTSIndex
        idx_engine = CodeFTSIndex(target_dir)
        idx_engine.index_files()
        results = idx_engine.search(query, limit=limit)
        if not results:
            print(f"No matches found for '{query}'.")
        else:
            print(f"\n\033[1m\033[36m⚡ SQLite FTS5 BM25 Search ({len(results)} matches for '{query}'):\033[0m\n")
            for r in results:
                print(f"  \033[32m{r['file']}:{r['line']}\033[0m: {r['content']}")
            print()

    elif command in ("index", "fts-index"):
        target_dir = os.getcwd()
        if len(args) > 1 and not args[1].startswith("-"):
            target_dir = os.path.abspath(args[1])
        from src.fts_index import CodeFTSIndex
        idx_engine = CodeFTSIndex(target_dir)
        if "--clear" in args or "--reset" in args:
            idx_engine.clear()
            print(f"\033[32m✔ Cleared FTS index database for {target_dir}.\033[0m")
        else:
            stats = idx_engine.index_files()
            print(f"\n\033[1m\033[36m⚡ SQLite FTS5 Full-Text Indexer ⚡\033[0m")
            print(f"  Scanned files: {stats['scanned_files']}")
            print(f"  Updated files: {stats['updated_files']}")
            print(f"  Indexed lines: {stats['indexed_lines']:,}")
            print(f"  Duration:      {stats['duration_ms']:.2f} ms")
            print(f"  Index path:    {idx_engine.db_path}\n")

    elif command in ("graph", "nodes"):
        from src.graph_engine import GraphEngine
        target_dir = args[1] if len(args) > 1 else os.getcwd()
        graph = GraphEngine.get_instance(target_dir)
        print(f"\n🔍 TokenSafe Graph Engine — Knowledge Graph & Centrality Analysis")
        print(f"   Indexed: {len(graph.file_symbols)} code files, {len(graph.symbols)} symbols\n")
        god_nodes = graph.get_god_nodes(top_n=7)
        print("⚡ Architectural Hubs (God Nodes):")
        for idx, g in enumerate(god_nodes, 1):
            print(f"  {idx}. {g['file']}")
            print(f"     Degree Centrality: {g['centrality']} | Dependents: {g['dependents']} | Dependencies: {g['dependencies']} | Symbols: {g['symbols_count']}")
        print(f"\n💡 Tip: Run 'tokensafe map' to generate tokensafe-out/API_SURFACE.md\n")

    elif command in ("map", "surface"):
        from src.graph_engine import GraphEngine
        target_dir = args[1] if len(args) > 1 else os.getcwd()
        if not os.path.isdir(target_dir):
            print(f"\n\033[31m✖ Directory not found: {target_dir}\033[0m\n")
            sys.exit(1)
        graph = GraphEngine.get_instance(target_dir)
        out_file = os.path.join(os.path.abspath(target_dir), "tokensafe-out", "API_SURFACE.md")
        graph.generate_api_surface(out_file)
        if not os.path.exists(out_file):
            print(f"\n\033[31m✖ Could not write API Surface Map to: {out_file}\033[0m\n")
            sys.exit(1)
        print(f"\n✓ Generated API Surface Map: {out_file} ({os.path.getsize(out_file):,} bytes)")
        print(f"  AI agents can read this file in ~1.5k tokens instead of reading the entire repo.\n")

    elif command == "skeleton":
        from src.graph_engine import GraphEngine
        if len(args) < 2:
            print("Usage: tokensafe skeleton <file_path> [target_symbol]")
            sys.exit(1)
        file_path = os.path.abspath(args[1])
        target_symbol = args[2] if len(args) > 2 else None
        graph, _ = GraphEngine.for_file(file_path)
        res = graph.graph_guided_skeletonize(file_path, target_symbol)
        print(res)

    elif command in ("login", "auth", "connect"):
        from src.license_manager import LicenseManager
        import json
        import time
        import urllib.request
        import webbrowser
        import platform

        cloud_url = os.environ.get("TOKENSAFE_CLOUD_URL", "https://api.tokensafe.dev").rstrip("/")
        active_tier, active_lic = LicenseManager.get_active_license()
        active_email = active_lic.get("email", "")

        print("\n\033[1m\033[36m╔════════════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1m\033[36m║             ⚡ TOKENSAFE ACCOUNT AUTHORIZATION ⚡                  ║\033[0m")
        print("\033[1m\033[36m╚════════════════════════════════════════════════════════════════════╝\033[0m\n")

        if active_email and active_email != "community@tokensafe.dev":
            print(f"  Currently logged in as: \033[32m{active_email}\033[0m (Tier: {active_tier.upper()})")
            if sys.stdin.isatty():
                ans = input("  Do you want to re-authenticate or switch accounts? [y/N]: ").strip().lower()
                if ans not in ("y", "yes"):
                    print("  Keeping current authorization.\n")
                    sys.exit(0)

        # 1. Request device code
        from src.version_checker import CURRENT_VERSION
        machine_fp = LicenseManager.get_machine_fingerprint()
        payload = {
            "machine_fp": machine_fp,
            "platform": platform.system()[:32],
            "arch": platform.machine()[:32],
            "agent": str(LicenseManager.detect_agent())[:32],
            "version": CURRENT_VERSION[:32],
            "hostname": platform.node()[:64]
        }
        try:
            req = urllib.request.Request(
                f"{cloud_url}/api/auth/device/code",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "TokenSafe/CLI-Login"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                init_data = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:
            print(f"\033[31m✖ Could not connect to {cloud_url}: {exc}\033[0m")
            print("  Check your internet connection and try again.\n")
            sys.exit(1)

        if not (init_data.get("success") or init_data.get("ok")):
            print(f"\033[31m✖ Authorization initiation failed: {init_data.get('error')}\033[0m\n")
            sys.exit(1)

        user_code = init_data["user_code"]
        device_code = init_data["device_code"]
        verif_url = init_data.get("verification_uri_complete") or f"{init_data['verification_uri']}?code={user_code}"
        interval = init_data.get("interval", 3)

        print("  To authorize this device and connect your account:")
        print(f"    1. Confirm code:  \033[1m\033[33m{user_code}\033[0m")
        print(f"    2. In browser at: {verif_url}\n")

        # Attempt to open browser automatically
        try:
            webbrowser.open(verif_url)
            print("  \033[2m(Opened verification page in your default browser)\033[0m")
        except Exception:
            pass

        print("  Waiting for authorization in browser (Ctrl+C to abort)...", end="", flush=True)

        # 2. Polling loop
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spin_idx = 0
        approved_data = None

        try:
            start_time = time.time()
            while time.time() - start_time < 600:
                time.sleep(interval)
                spin_char = spinner[spin_idx % len(spinner)]
                spin_idx += 1
                print(f"\r  {spin_char} Waiting for authorization in browser (Ctrl+C to abort)...", end="", flush=True)

                try:
                    poll_req = urllib.request.Request(
                        f"{cloud_url}/api/auth/device/poll",
                        data=json.dumps({"device_code": device_code}).encode("utf-8"),
                        headers={"Content-Type": "application/json", "User-Agent": "TokenSafe/CLI-Login"},
                        method="POST"
                    )
                    with urllib.request.urlopen(poll_req, timeout=4.0) as resp:
                        poll_data = json.loads(resp.read().decode("utf-8") or "{}")
                except Exception:
                    continue

                status = poll_data.get("status")
                if status == "approved":
                    approved_data = poll_data
                    break
                elif status == "quota_exhausted":
                    print(f"\n\n\033[31m✖ {poll_data.get('error')}\033[0m\n")
                    sys.exit(1)
                elif status in ("expired", "denied"):
                    print(f"\n\n\033[31m✖ Authorization {status}.\033[0m\n")
                    sys.exit(1)

        except KeyboardInterrupt:
            print("\n\n\033[33m  Authorization cancelled by user.\033[0m")
            print("  Run 'tokensafe login' whenever you are ready.\n")
            sys.exit(130)

        if not approved_data:
            print("\n\n\033[31m✖ Authorization timed out. Please try again.\033[0m\n")
            sys.exit(1)

        # 3. Save license and device token
        lic_key = approved_data.get("license_key", "")
        dev_tok = approved_data.get("device_token", "")
        auth_email = approved_data.get("email") or approved_data.get("user_email") or ""

        ok, msg = LicenseManager.save_license(lic_key, device_token=dev_tok)
        if ok:
            valid, lic_meta, _ = LicenseManager.validate_license(lic_key)
            detected_tier = (lic_meta.get("tier") or approved_data.get("tier") or "free").lower()
            org_id = lic_meta.get("org")

            print(f"\r\033[K  \033[32m\033[1m✔ Successfully authenticated as {auth_email}!\033[0m")
            if detected_tier == "solo":
                print("  \033[32m✔ Solo Pro license activated (Unlimited tokens).\033[0m")
            elif detected_tier in ("team", "enterprise"):
                org_msg = f" [{org_id}]" if org_id else ""
                print(f"  \033[32m✔ Team license activated (Unlimited tokens, organization workspace{org_msg}).\033[0m")
            else:
                print("  \033[32m✔ Community Edition license activated (500,000 free tokens/month).\033[0m")
            print(f"  \033[36m✔ Web dashboard unlocked at:\033[0m https://tokensafe.dev/dashboard\n")

            # Cloud Prompt Booster consent (paid tiers only; already-chosen users skipped)
            if detected_tier in ("solo", "team", "enterprise"):
                _booster_consent_prompt()

            # Sync local token metrics
            try:
                from src.telemetry_sync import TelemetrySync
                sync_res = TelemetrySync.sync(license_key=lic_key, timeout=3.0)
                if sync_res.get("success") and sync_res.get("synced_events", 0) > 0:
                    print(f"  \033[36m✔ Synced {sync_res['synced_events']:,} optimization events to your cloud dashboard.\033[0m\n")
            except Exception:
                pass
        else:
            print(f"\n\033[31m✖ Failed to save license: {msg}\033[0m\n")
            sys.exit(1)

    elif command in ("logout", "unlink", "disconnect"):
        from src.license_manager import LicenseManager, LICENSE_FILE, GLOBAL_LICENSE_FILE
        import json
        import urllib.request

        cloud_url = os.environ.get("TOKENSAFE_CLOUD_URL", "https://api.tokensafe.dev").rstrip("/")
        machine_fp = LicenseManager.get_machine_fingerprint()
        _, active_lic = LicenseManager.get_active_license()
        active_email = active_lic.get("email", "")

        # 1. Unlink on server to free up device slot
        if active_email and machine_fp and active_email != "community@tokensafe.dev":
            try:
                req = urllib.request.Request(
                    f"{cloud_url}/api/auth/device/unlink",
                    data=json.dumps({"email": active_email, "machine_fp": machine_fp}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "TokenSafe/CLI-Logout"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=4.0):
                    pass
            except Exception:
                pass

        # 2. Remove local license files
        for p in LicenseManager._candidate_license_files():
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        print("\n\033[32m✔ Successfully unlinked this device and logged out.\033[0m")
        if active_email and active_email != "community@tokensafe.dev":
            print(f"  Disconnected account: {active_email}")
        print("  Your device slot has been freed.")
        print("  Run \033[36mtokensafe login\033[0m anytime to connect again.\n")

    elif command == "activate":
        from src.license_manager import LicenseManager
        if len(args) < 2:
            print("Usage: tokensafe activate <LICENSE_KEY>")
            sys.exit(1)
        key = args[1].strip()
        ok, msg = LicenseManager.save_license(key)
        if ok:
            print(f"\n\033[32m\033[1m✔ {msg}\033[0m")
            # Cloud Prompt Booster consent (paid tiers only; already-chosen users skipped)
            try:
                valid, lic_meta, _ = LicenseManager.validate_license(key)
                if valid and str(lic_meta.get("tier", "")).lower() in ("solo", "team", "enterprise"):
                    _booster_consent_prompt()
            except Exception:
                pass
            # Automatically report version and seat activation to cloud
            try:
                from src.version_checker import VersionChecker
                VersionChecker.check_version(license_key=key, force_fetch=True)
            except Exception:
                pass
            # Automatically sync existing local token metrics to newly activated license
            try:
                from src.telemetry_sync import TelemetrySync
                sync_res = TelemetrySync.sync(license_key=key, timeout=3.0)
                if sync_res.get("success") and sync_res.get("synced_events", 0) > 0:
                    print(f"  \033[36m✔ Synced {sync_res['synced_events']:,} past optimization events to cloud dashboard.\033[0m")
            except Exception:
                pass
            print()
        else:
            print(f"\n\033[31m\033[1m✖ {msg}\033[0m\n")
            sys.exit(1)

    elif command in ("sync", "push"):
        from src.telemetry_sync import TelemetrySync
        print("\n\033[1m\033[36m⚡ SYNCHRONIZING TOKEN METRICS WITH DASHBOARD ⚡\033[0m\n")
        force_sync = "--force" in args or "-f" in args
        status_before = TelemetrySync.get_sync_status()
        print(f"  Local recorded events: {status_before['total_local_events']:,}")
        print(f"  Pending sync:          {status_before['pending_events']:,}\n")
        res = TelemetrySync.sync(force=force_sync, verbose=True)
        if res.get("success"):
            print(f"  \033[32m✔ {res.get('message', 'Synchronized successfully.')}\033[0m")
            status_after = TelemetrySync.get_sync_status()
            print(f"  Total synced events:   {status_after['total_synced_events']:,}\n")
        else:
            print(f"  \033[31m✖ Sync failed: {res.get('error', 'Unknown error')}\033[0m\n")

    elif command in ("license", "tier"):
        from src.license_manager import LicenseManager
        tier, payload = LicenseManager.get_active_license()
        print("\n\033[1m\033[36m⚡ TOKENSAFE LICENSE STATUS ⚡\033[0m\n")
        print(f"  Tier:       \033[32m\033[1m{tier.upper()}\033[0m")
        if tier == "guest":
            quota = LicenseManager.check_quota()
            q_saved = int(quota.get("monthly_saved", 0))
            q_limit = int(quota.get("monthly_limit") or 50_000)
            q_state = "\033[31mQUOTA REACHED (Optimizers paused)\033[0m" if quota.get("is_quota_exceeded") else "\033[32mActive\033[0m"
            print(f"  Status:     Guest Mode ({q_saved:,} / {q_limit:,} tokens this month · {q_state})")
            print("  Mode:       Local Offline (No account connected)")
            print("  Unlock 500k: \033[36mtokensafe login\033[0m (Free Community license + Web Dashboard)")
            print("  Solo Pro:   https://tokensafe.dev/signup (Unlimited tokens)\n")
        elif tier == "free":
            quota = LicenseManager.check_quota()
            q_saved = int(quota.get("monthly_saved", 0))
            q_limit = int(quota.get("monthly_limit") or 500_000)
            q_state = "\033[31mQUOTA REACHED (Pro optimizations paused)\033[0m" if quota.get("is_quota_exceeded") else "\033[32mActive\033[0m"
            print(f"  Email:      {payload.get('email', 'community@tokensafe.dev')}")
            print(f"  Status:     Community Account ({q_saved:,} / {q_limit:,} measured tokens this month · {q_state})")
            print("  Upgrade:    https://tokensafe.dev/signup ($9/mo Solo with 7-Day Free Trial)\n")
        else:
            print(f"  Email:      {payload.get('email', 'N/A')}")
            if payload.get("trial"):
                print(f"  Mode:       \033[33m7-Day Free Trial\033[0m")
            exp = payload.get("expires_at", 0)
            if exp > 0:
                import datetime
                dt = datetime.datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  Expires:    {dt}")
            else:
                print(f"  Expires:    Never (Lifetime)")
        print()

    elif command in ("booster",):
        from src.license_manager import LicenseManager
        from src.distiller import UniversalDistiller
        from src.egress_counter import today_counts
        sub = args[1].strip().lower() if len(args) > 1 else "status"
        tier, _ = LicenseManager.get_active_license()
        print("\n\033[1m\033[36m⚡ CLOUD PROMPT BOOSTER ⚡\033[0m\n")
        if tier not in UniversalDistiller.PAID_TIERS:
            print("  Cloud Prompt Booster distills long prompts (100+ tokens) cloud-side with")
            print("  ~5x deeper compression than the local engine. Available on Solo, Team, and Enterprise.")
            print("  Guest and Community tiers keep all prompt processing 100% local by design.\n")
        elif sub in ("on", "enable", "off", "disable"):
            value = sub in ("on", "enable")
            if UniversalDistiller.write_config_value("cloud_distill", value):
                if value:
                    print("  \033[32m✔ Booster enabled\033[0m — long prompts are distilled via TokenSafe Cloud")
                    print("    (secrets scrubbed before egress; raw text never stored; not used for training).")
                else:
                    print("  \033[33m○ Booster disabled\033[0m — all prompt processing stays 100% local.")
            else:
                print("  \033[31m✖ Failed to write data/config.json — check permissions.\033[0m")
            print()
        else:
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled(tier)
            eg = today_counts()
            state = "\033[32mon\033[0m" if enabled else "\033[33moff\033[0m"
            if not explicitly_set:
                state += " \033[2m(active by default — make an explicit choice below)\033[0m"
            print(f"  Status:    {state}")
            print(f"  Egress:    today — {eg['booster']} booster · {eg['telemetry']} telemetry outbound requests")
            print("  What it sends when on: prompt text (code snippets/stack traces possible) with secrets")
            print("  scrubbed → TokenSafe Cloud → Google Gemini Flash-Lite. Transient processing;")
            print("  raw text never stored, not used for training.")
            print("  Toggle:    \033[36mtokensafe booster on|off\033[0m\n")

    elif command in ("providers", "models", "keys"):
        from src.license_manager import LicenseManager
        from src.distiller import UniversalDistiller
        tier, _ = LicenseManager.get_active_license()
        print("\n\033[1m\033[36m⚡ TOKENSAFE LLM DISTILLATION PROVIDERS ⚡\033[0m\n")
        if tier not in UniversalDistiller.PAID_TIERS:
            tier_label = "Guest (Offline)" if tier == "guest" else "Free Community Edition"
            print(f"  \033[33mTier: {tier_label}\033[0m")
            print("  Cloud Prompt Booster (managed distillation, zero API keys) is reserved for Solo, Team, and Enterprise tiers.")
            print("  Guest and Community Editions operate with the zero-latency, zero-cost Local Regex Compactor.\n")
            print("  \033[1mTo unlock the Cloud Prompt Booster:\033[0m")
            print("  1. Start 7-day free trial or upgrade at: https://tokensafe.dev")
            print("  2. Activate your key: \033[32mtokensafe activate <KEY>\033[0m\n")
        elif not UniversalDistiller.is_self_hosted():
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled(tier)
            if enabled:
                print(f"  \033[32m✔\033[0m \033[1mCloud Prompt Booster\033[0m [Active: gemini-flash-lite-latest via TokenSafe Cloud]")
                print("    Long prompts (100+ tokens) are distilled cloud-side. Secrets are scrubbed before")
                print("    egress; raw text is never stored and is not used for training. Zero API keys required.")
                if not explicitly_set:
                    print("    \033[2mActive by default for your plan — make an explicit choice: tokensafe booster on|off\033[0m")
            else:
                print("  \033[33m○ Cloud Prompt Booster: off\033[0m — long prompts stay on the local regex compactor.")
                print("    Enable anytime: \033[36mtokensafe booster on\033[0m")
            print()
            print("  \033[2m💡 Tip: To run self-hosted with custom BYOK keys in Docker, export TOKENSAFE_SELF_HOSTED=1.\033[0m\n")
        else:
            print("  \033[36mMode: Self-Hosted Docker (Custom BYOK Keys)\033[0m\n")
            configured = UniversalDistiller.get_configured_providers()
            all_p = UniversalDistiller.PROVIDERS
            for key, p in all_p.items():
                if key in configured:
                    c = configured[key]
                    print(f"  \033[32m✔\033[0m \033[1m{p['name']:<18}\033[0m [Active: {c['model']}]")
                else:
                    print(f"  \033[2m○ {p['name']:<18} [Set {p['env_key']} to activate]\033[0m")
            print("\nSet environment variable or add to data/config.json to activate.\n")

    elif command in ("markdown", "md", "export"):
        print(Dashboard(MetricsTracker()).render_markdown())

    elif command == "test":
        if len(args) < 2:
            print("Usage: tokensafe test \"<prompt text>\"")
            sys.exit(1)
        raw_prompt = " ".join(args[1:])
        res = PromptCompactor.compact(raw_prompt)

        print("\n\033[1m\033[36m⚡ TOKEN COMPRESSION TEST ⚡\033[0m\n")
        print(f"\033[1mOriginal Input:\033[0m  {res['original_text']}")
        print(f"\033[1mCompacted DSL:\033[0m   \033[32m\033[1m{res['compact_text']}\033[0m\n")
        print(f"\033[1mRouting Tier:\033[0m   \033[33m{res.get('tier', 'REGEX')}\033[0m")
        print(f"\033[1mExtracted Action:\033[0m {res['action']}")
        print(f"\033[1mTarget File:\033[0m      {res['target_file'] or 'None'}")
        print(f"\033[1mScope Symbol:\033[0m     {res['symbol'] or 'None'}")
        print(f"\033[1mTraits / Tags:\033[0m    {res['traits']}\n")
        print(f"\033[1mOriginal Tokens:\033[0m  {res['original_tokens']}")
        print(f"\033[1mCompacted Tokens:\033[0m {res['compact_tokens']}")
        print(f"\033[1mTokens Saved:\033[0m     \033[32m\033[1m+{res['saved_tokens']} tokens\033[0m ({render_bar(res['reduction_pct'])})\n")

    elif command == "wrap":
        if len(args) < 2:
            print("Usage: tokensafe wrap <command>")
            sys.exit(1)
        sub_cmd = " ".join(args[1:])
        try:
            proc = subprocess.run(sub_cmd, shell=True, capture_output=True, text=True)
            raw = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            print(f"Error running command: {e}")
            sys.exit(1)

        from src.format_parsers.ansi import strip_ansi
        from src.test_parsers import TestOutputParser
        from src.build_parsers import BuildOutputParser
        from src.stash import stash_block

        raw = strip_ansi(raw)
        compacted, was_test = TestOutputParser.compact(raw)
        if was_test:
            print(compacted)
            sys.exit(proc.returncode)

        compacted_build, was_build = BuildOutputParser.compact(raw, exit_code=proc.returncode, cmd=sub_cmd)
        if was_build:
            print(compacted_build)
            sys.exit(proc.returncode)

        # Container build reducer (docker build, docker compose build)
        try:
            from src.format_parsers.docker_k8s import ContainerAndK8sReducer
            compacted_docker, was_docker = ContainerAndK8sReducer.reduce_docker_build(raw)
            if was_docker:
                print(compacted_docker)
                sys.exit(proc.returncode)
        except Exception:
            pass

        # Structured NDJSON / JSON log reducer
        try:
            from src.format_parsers.json_log import JSONLogReducer
            if JSONLogReducer.is_ndjson_log(raw):
                compacted_logs, was_logs = JSONLogReducer.reduce(raw)
                if was_logs:
                    print(compacted_logs)
                    sys.exit(proc.returncode)
        except Exception:
            pass

        lines = raw.splitlines()
        if len(lines) > 40:
            head = lines[:15]
            tail = lines[-15:]
            omitted = len(lines) - 30
            _, marker = stash_block(raw, source="wrap")
            folded = "\n".join(head + [f"... [{omitted} lines omitted by TokenSafe] ..."] + tail + [marker])
            print(folded)
        else:
            print(raw)
        sys.exit(proc.returncode)

    elif command in ("tests", "testsuite", "unittest"):
        tests_dir = os.path.join(PROJECT_ROOT, "tests")
        if not os.path.isdir(tests_dir):
            print("No tests/ directory found — run from a repository checkout "
                  "(installed packages do not ship the unit-test suite).")
            sys.exit(1)
        verbose = "--verbose" in args or "-v" in args
        full_tb = "--traceback" in args or "-tb" in args
        launcher = next((a for a in args[1:] if not a.startswith("-")), "unittest")
        if launcher == "unittest":
            cmd = [sys.executable, "-m", "unittest", "discover", "tests"]
        elif launcher == "pytest":
            # pytest is natively compact with quiet flags; the wrapper
            # guarantees them instead of trusting the caller.
            cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
        else:
            print(f"Unknown test launcher '{launcher}' — supported: unittest (default), pytest.")
            sys.exit(1)
        print(f"\n⚡ Running the unit test suite ({launcher})...")
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
        except FileNotFoundError:
            print(f"Launcher '{launcher}' is not available — install it (e.g. pip install pytest) and retry.")
            sys.exit(1)
        raw = (proc.stdout or "") + (proc.stderr or "")
        if verbose:
            print(raw)
        elif launcher == "pytest":
            print(raw.rstrip() or "(no output)")
        else:
            print(summarize_unittest_output(raw, full_traceback=full_tb))
        sys.exit(proc.returncode)

    elif command in ("prune", "cleanup"):
        days = None
        max_events = None
        i = 1
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
                i += 2
            elif args[i] in ("--max", "--max-events") and i + 1 < len(args):
                try:
                    max_events = int(args[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1

        if days is None and max_events is None:
            days = 30
            max_events = 5000

        print(f"\n\033[1m\033[36m⚡ DATA RETENTION PRUNE ⚡\033[0m")
        print(f"Policy: retain events < {days or 'any'} days old, max {max_events or 'unlimited'} entries.")
        res = MetricsTracker().prune(days=days, max_events=max_events, vacuum=True)
        b_before = res.get("db_size_before", 0) / 1024.0
        b_after = res.get("db_size_after", 0) / 1024.0
        print(f"\033[32m✔ Pruned {res['pruned_events']} granular events into lifetime rollup table.\033[0m")
        print(f"  DB Size: {b_before:.1f} KB ➔ {b_after:.1f} KB\n")

    elif command == "vacuum":
        print("\n\033[1m\033[36m⚡ VACUUMING METRICS DATABASE ⚡\033[0m")
        t = MetricsTracker()
        b_before = (os.path.getsize(t.db_path) if os.path.exists(t.db_path) else 0) / 1024.0
        t.vacuum()
        # Repair pass: rebuild session aggregates straight from events (guards
        # against sessions/events drift after crashes or manual DB edits).
        t.recalculate_sessions()
        b_after = (os.path.getsize(t.db_path) if os.path.exists(t.db_path) else 0) / 1024.0
        print(f"\033[32m✔ Database compacted + session aggregates rebuilt. Size: {b_before:.1f} KB ➔ {b_after:.1f} KB\033[0m\n")

    elif command in ("expand-ref", "expand_ref", "expand"):
        if len(args) < 2:
            print("Usage: tokensafe expand-ref <hash8>")
            sys.exit(1)
        ref_hash = args[1].strip()
        from src.stash import retrieve_block
        content = retrieve_block(ref_hash)
        if content is None:
            print(f"\033[31mError: Stashed reference '{ref_hash}' not found or expired.\033[0m")
            sys.exit(1)
        print(content)

    elif command in ("optimize-image", "image-opt", "image"):
        if len(args) < 2:
            print("Usage: tokensafe optimize-image <image_path> [--max-edge 1024]")
            sys.exit(1)
        img_path = args[1]
        max_edge = 1024
        if "--max-edge" in args:
            idx = args.index("--max-edge")
            if idx + 1 < len(args):
                try:
                    max_edge = int(args[idx + 1])
                except ValueError:
                    pass
        from src.image_downscale import ImageDownscaler
        res = ImageDownscaler.optimize(img_path, max_edge=max_edge)
        if not res.get("success"):
            print(f"\033[31mError: {res.get('error')}\033[0m")
            sys.exit(1)
        if res.get("was_downscaled"):
            print(f"\033[32m✔ Downscaled image using {res.get('engine')}:\033[0m {res['orig_dimensions']} ➔ {res['new_dimensions']}")
            print(f"  Saved vision tokens: ~{res['saved_tokens']} tokens")
            print(f"  Optimized image: {res['optimized_path']}")
        else:
            print(f"Image within bounds ({res.get('dimensions', 'N/A')} <= {max_edge}px). Using original file.")

    elif command in ("prune-superseded", "prune_superseded"):
        if len(args) < 2:
            print("Usage: tokensafe prune-superseded <transcript_path>")
            sys.exit(1)
        t_path = args[1]
        from src.superseded import SupersededPruner
        res = SupersededPruner.prune_transcript(t_path)
        if not res.get("success"):
            print(f"\033[31mError: {res.get('error')}\033[0m")
            sys.exit(1)
        print(f"\033[32m✔ Pruned {res['pruned_count']} superseded tool read(s) from transcript.\033[0m")
        print(f"  Saved context: ~{res['saved_tokens']} tokens")

    elif command in ("mistakes", "deadends"):
        from src.mistakes import MistakesLedger
        sub = args[1] if len(args) > 1 else "list"
        if sub in ("clear", "reset"):
            c = MistakesLedger.clear_mistakes(os.getcwd())
            print(f"\033[32m✔ Cleared {c} mistake record(s).\033[0m")
        elif sub in ("record", "add"):
            reason = "Failed hypothesis"
            f_path = None
            if "--reason" in args:
                idx = args.index("--reason")
                if idx + 1 < len(args):
                    reason = args[idx + 1]
            if "--file" in args:
                idx = args.index("--file")
                if idx + 1 < len(args):
                    f_path = args[idx + 1]
            entry = MistakesLedger.record_revert(f_path or "project", reason, project_root=os.getcwd())
            print(f"\033[32m✔ Recorded mistake: {entry['summary']}\033[0m")
        else:
            entries = MistakesLedger.list_mistakes(project_root=os.getcwd())
            if not entries:
                print("No recorded dead-ends or reverts for this project.")
            else:
                print(f"\n\033[1m\033[36m⚡ TOKENSAFE ANTI-STUCK DEAD-ENDS LEDGER ({len(entries)}) ⚡\033[0m\n")
                for e in entries:
                    print(f"  • \033[1m{e.get('file') or 'project'}\033[0m: {e.get('summary')} [{e.get('source')}]")
                guard = MistakesLedger.get_guard_prompt(project_root=os.getcwd())
                if guard:
                    print(f"\n\033[33mActive prompt guard:\033[0m\n{guard}\n")

    elif command == "reset":
        confirm = input("Are you sure you want to reset all token metrics? [y/N]: ").strip().lower()
        if confirm == "y":
            MetricsTracker().reset()
            print("Metrics database reset successfully.")
        else:
            print("Reset cancelled.")

    else:
        print(f"Unknown command: {command}")
        print("Available commands: init, login, logout, stats, status, sync, html, test, tests, expand-ref, wrap, web, doctor, skills, providers, mcp, benchmark, proxy, graph, map, skeleton, guard, mistakes, vacuum, prune, digest, activate, license, markdown, install, uninstall, setup, update, check-update, reset. Run 'tokensafe help' for details.")
        sys.exit(1)

if __name__ == "__main__":
    main()
