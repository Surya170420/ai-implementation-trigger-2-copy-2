# uv run -m hygiene_check.model_bench --models minimax-m2.5:cloud --runs 3 --date '2026-07-28' --hours 5,6,7,8
"""Benchmark several models against the SAME hygiene-check window, repeated
N times each, and report per-model reliability/latency so model choice is
based on numbers instead of a vibe from one noisy run.

Why repeat runs at all: a single run's failure count is noisy (timeouts,
rate limits, a model having a bad moment). Repeating N times per model and
aggregating gives a stable read on each model's actual JSON-compliance rate
against your real prompts/evidence.

Each (model, run) goes through hygiene_check.pipeline.main() with:
  --model      swap the model for this run only
  --out        its own output dir, so runs don't overwrite each other
  --state-file its own fresh state file, so case recurrence from run 3
               doesn't start force-escalating things that have nothing to
               do with which model you're testing

Note: this repo's pipeline.main() already defaults to NOT sending real
email (--send-email is opt-in, off by default) -- a benchmark sweep must
never pass --send-email.

Usage:
    python -m hygiene_check.model_bench \\
        --models gemma4:31b-cloud gpt-oss:120b-cloud minimax-m2.5:cloud \\
        --runs 10

    # narrow the window so every run/model hits the same fixed data:
    python -m hygiene_check.model_bench --models m1 m2 --runs 20 \\
        --date 2026-07-22 --hours 14,15,16,17

    # cap spend: 3 models x 10 runs can add up fast
    python -m hygiene_check.model_bench --models m1 m2 --runs 10 --max-calls 40

Output:
    out/bench/<model>/run_<n>/report.json   (unchanged pipeline output, per run)
    out/bench/summary.json                  aggregated stats per model
    a comparison table printed to stdout
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from hygiene_check.pipeline import main as pipeline_main

ROOT = Path(__file__).parent.parent
DEFAULT_LOG_FILE = ROOT / "llm_calls.jsonl"


def _safe_name(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", model)


def _read_call_log(path: Path, since_ts: float, until_ts: float, model: str) -> list[dict]:
    """Pull raw generate() call records (latency, token usage) logged by
    ai_toolkit.observability during this run's time window for this model.
    Best-effort: an empty/missing log just means no latency stats, not a
    failed run."""
    if not path.exists():
        return []
    import datetime
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("model") != model:
                continue
            try:
                ts = datetime.datetime.fromisoformat(rec["ts"]).timestamp()
            except (KeyError, ValueError):
                continue
            if since_ts <= ts <= until_ts:
                out.append(rec)
    return out


def _run_once(model: str, run_idx: int, config: str, date: str | None,
              hours: str | None, max_calls: int | None) -> dict:
    out_dir = f"out/bench/{_safe_name(model)}/run_{run_idx}"
    state_file = f"{out_dir}/state.json"

    # deliberately no --send-email: benchmark runs must never hit a real inbox
    argv = ["--config", config, "--model", model, "--out", out_dir,
            "--state-file", state_file]
    if date:
        argv += ["--date", date]
    if hours:
        argv += ["--hours", hours]
    if max_calls is not None:
        argv += ["--max-calls", str(max_calls)]

    started_wall = time.time()
    error = None
    try:
        pipeline_main(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            error = f"pipeline exited with code {exc.code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    ended = time.time()

    report_path = ROOT / out_dir / "report.json"
    stats = {
        "model": model, "run": run_idx, "error": error,
        "wall_time_s": round(ended - started_wall, 2),
        "investigated_rows": 0, "failed_llm_rows": 0,
        "true_positive_rows": 0, "false_positive_rows": 0,
        "cases": 0, "escalated_cases": 0,
        "call_count": 0, "avg_latency_s": None, "p95_latency_s": None,
        "avg_completion_tokens": None,
    }
    if report_path.exists():
        report = json.loads(report_path.read_text())
        for case in report.get("cases", []):
            stats["cases"] += 1
            if case.get("escalation_reasons"):
                stats["escalated_cases"] += 1
            stats["true_positive_rows"] += case.get("true_positive_count") or 0
            failed = case.get("failed_investigation_count") or 0
            stats["failed_llm_rows"] += failed
            for rv in case.get("row_verdicts") or []:
                v = rv.get("verdict") or {}
                if v.get("true_positive") is False:
                    stats["false_positive_rows"] += 1
                if "error" not in v:
                    stats["investigated_rows"] += 1
    elif error is None:
        stats["error"] = "no report.json written (did the run actually investigate anything?)"

    calls = _read_call_log(DEFAULT_LOG_FILE, started_wall, ended, model)
    if calls:
        latencies = [c["latency_s"] for c in calls if c.get("latency_s") is not None]
        tokens = [c["usage"]["completion_tokens"] for c in calls
                  if c.get("usage", {}).get("completion_tokens")]
        stats["call_count"] = len(calls)
        if latencies:
            stats["avg_latency_s"] = round(statistics.mean(latencies), 2)
            if len(latencies) > 1:
                stats["p95_latency_s"] = round(
                    statistics.quantiles(latencies, n=20)[18], 2)
        if tokens:
            stats["avg_completion_tokens"] = round(statistics.mean(tokens), 1)

    return stats


def _aggregate(model: str, runs: list[dict]) -> dict:
    total_rows = sum(r["investigated_rows"] + r["failed_llm_rows"] for r in runs)
    total_failed = sum(r["failed_llm_rows"] for r in runs)
    latencies = [r["avg_latency_s"] for r in runs if r["avg_latency_s"] is not None]
    run_errors = [r for r in runs if r["error"]]
    return {
        "model": model,
        "runs": len(runs),
        "runs_with_error": len(run_errors),
        "total_rows_attempted": total_rows,
        "total_llm_call_failures": total_failed,
        "call_failure_rate": round(total_failed / total_rows, 3) if total_rows else None,
        "total_true_positive_rows": sum(r["true_positive_rows"] for r in runs),
        "total_false_positive_rows": sum(r["false_positive_rows"] for r in runs),
        "total_escalated_cases": sum(r["escalated_cases"] for r in runs),
        "avg_latency_s": round(statistics.mean(latencies), 2) if latencies else None,
        "run_errors": [r["error"] for r in run_errors],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark models against hygiene_check")
    parser.add_argument("--models", nargs="+", required=True,
                        help="model names to benchmark, e.g. gemma4:31b-cloud gpt-oss:120b-cloud")
    parser.add_argument("--runs", type=int, default=10,
                        help="repeats per model (default 10)")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--date", help="explicit window date YYYY-MM-DD (with --hours) -- "
                        "recommended so every model/run hits the same fixed data")
    parser.add_argument("--hours", help="explicit window hours, e.g. 6,7,8,9")
    parser.add_argument("--max-calls", type=int,
                        help="cap llm.max_calls_per_run for each individual run")
    args = parser.parse_args(argv)
    
    all_stats: dict[str, list[dict]] = {m: [] for m in args.models}

    for model in args.models:
        for run_idx in range(1, args.runs + 1):
            print(f"[bench] {model} run {run_idx}/{args.runs}...", flush=True)
            stats = _run_once(model, run_idx, args.config, args.date, args.hours, args.max_calls)
            all_stats[model].append(stats)
            status = stats["error"] or "ok"
            print(f"  -> {status} | investigated={stats['investigated_rows']} "
                  f"failed={stats['failed_llm_rows']} "
                  f"tp={stats['true_positive_rows']} fp={stats['false_positive_rows']} "
                  f"avg_latency={stats['avg_latency_s']}s")

    summary = {m: _aggregate(m, runs) for m, runs in all_stats.items()}
    out_path = ROOT / "out" / "bench" / "summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"per_run": all_stats, "summary": summary}, indent=2))

    print("\n=== model comparison ===")
    header = f"{'model':<28}{'runs':>6}{'call_fail_rate':>16}{'avg_latency_s':>15}{'tp':>6}{'fp':>6}{'escalated':>11}"
    print(header)
    for m, s in summary.items():
        cfr = "n/a" if s["call_failure_rate"] is None else f"{s['call_failure_rate']:.1%}"
        lat = "n/a" if s["avg_latency_s"] is None else f"{s['avg_latency_s']}"
        print(f"{m:<28}{s['runs']:>6}{cfr:>16}{lat:>15}"
              f"{s['total_true_positive_rows']:>6}{s['total_false_positive_rows']:>6}"
              f"{s['total_escalated_cases']:>11}")
    print(f"\n[bench] full detail written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
