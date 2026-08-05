"""Hygiene-check pipeline: validate -> fingerprint -> investigate -> report.

Run from the repo root:
    python -m hygiene_check.pipeline                 # full run (needs the LLM endpoint)
    python -m hygiene_check.pipeline --no-llm        # rules + grouping only
    python -m hygiene_check.pipeline --config path/to/other.yaml
    python -m hygiene_check.pipeline --no-llm --date '2026-07-12' --hours 1,2,3,4
    python -m hygiene_check.pipeline --date '2026-07-01'

Investigation is a two-stage LLM flow per row/case (see hygiene_check/investigator.py):
    1. extract_columns()      mechanical per-column HTML-vs-stored comparison
    2. validate_holistically() true/false-positive call across all columns

Granularity (see VISIBILITY_PER_ROW_CAP below):
    - visibility cases with failed_count <= cap: one evidence bundle + one
      LLM pass PER FAILING ROW, each checked against its own product's HTML
    - OSA cases, and visibility cases over the cap: one evidence bundle for
      the case (a few representative rows), like the original design

Escalation: floor/recurrence triggers fire unconditionally (a validator LLM
mistake must not silently suppress a real, large-scale outage); the LLM's
true_positive verdict is an additional independent trigger, not a gate on
the others. Only cases with escalation_reasons appear in alert_payload.json
and the email; when floor/recurrence overrides a false_positive verdict,
that disagreement is flagged as its own reason (see decide_escalation()).

Budget: hygiene_check/budget.py enforces a soft cap on LLM calls per run
(llm.max_calls_per_run in config.yaml) and catches provider rate-limit/quota
errors. Either one checkpoints whatever has already been fully verdicted to
report.json/state.json and exits cleanly (not an error) — re-running the
same command resumes; already-processed cases are not re-investigated.
This budgets the pipeline's OWN LLM provider calls — it has no visibility
into a chat session's context window.

Outputs in out/:
    report.json        every case with counts, samples, row_verdicts (or None
                        for cases skipped by --no-llm or a checkpoint)
    alert_payload.json escalations only — feed this to the org mailer
    <case_id>.json      per-case evidence artifact, written before any LLM
                        call so it exists even if investigation fails/pauses
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ai_toolkit.checks import fingerprint, load_expectations, run_checks
from ai_toolkit.config import load_config
from hygiene_check.data_sources import load_entries
from hygiene_check.evidence import build_case_evidence, build_row_evidence
from hygiene_check.investigator import extract_columns, validate_holistically
from hygiene_check.budget import CallBudget, BudgetExceeded, is_rate_limit_error
from hygiene_check.extractors.script_extractor import repo_handle
from common_utils_repository.mailer import mailer_script_outlook as mail
from .html_body.html_render import render_email_html
from dotenv import load_dotenv

load_dotenv()

# Visibility cases at/under this many failing rows get per-row evidence and
# per-row LLM calls (each row checked against its own product's HTML).
# Above it, falls back to case-level (few representative rows) like OSA,
# because per-row calls scale with failed_count and would blow the budget.
VISIBILITY_PER_ROW_CAP = 50

ROOT = Path(__file__).parent.parent
EXPECTATIONS_DIR = Path(__file__).parent / "expectations"


def severity_rank(case: dict) -> tuple:
    return (0 if case["severity"] == "error" else 1, -case["failed_count"])


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"case_streaks": {}}


def decide_escalation(case: dict, esc_cfg: dict) -> list[str]:
    """Runs for every investigated case, unconditionally. Strict
    true-positive gate: a case reaches the email if the LLM says true_positive:
    true at or above min_confidence. This is the primary escalation trigger.

    A second trigger escalates any case where the model's confidence is low
    (e.g., below 60%), regardless of the true_positive verdict. This ensures
    that uncertain results are surfaced for human review.

    An earlier version force-escalated on other metrics (failed_share, recurrence),
    but those have been removed to focus on AI-driven verdicts."""
    reasons = []

    verdict = case.get("verdict") or {}
    min_confidence = esc_cfg.get("min_confidence", 0.5)
    
    if verdict.get("true_positive") and verdict.get("confidence", 0.0) >= min_confidence:
        reasons.append("ai_verdict")
    
    if verdict.get("confidence", 1.0) < 0.6:
        if "low_confidence" not in reasons:
            reasons.append("low_confidence")

    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hygiene-check pipeline")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--no-llm", action="store_true",
                        help="run rules + fingerprinting only, skip AI investigation")
    parser.add_argument("--date", help="explicit window date YYYY-MM-DD (with --hours)")
    parser.add_argument("--hours", help="explicit window hours, e.g. 6,7,8,9")
    parser.add_argument("--model", help="override llm.model from the config file "
                        "(e.g. for benchmarking several models against the same window)")
    parser.add_argument("--out", help="override output_dir from the config file "
                        "(e.g. one directory per model/run so repeats don't overwrite each other)")
    parser.add_argument("--state-file", help="override escalation.state_file "
                        "(use a fresh path per benchmark run -- otherwise case "
                        "recurrence carries across repeated runs and pollutes results)")
    parser.add_argument("--max-calls", type=int, help="override llm.max_calls_per_run "
                        "(caps LLM spend for this run independent of the config file)")
    parser.add_argument("--send-email", action="store_true",
                        help="actually send the alert email via the configured mailer "
                        "(default: off -- report.json/alert_payload.json/email.html are "
                        "still written either way). Never pass this from a benchmark run.")
    args = parser.parse_args(argv)
    hours = [int(h) for h in args.hours.split(",")] if args.hours else None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

    cfg = load_config(args.config)
    if args.model and "providers" not in cfg.get("llm", {}):
        cfg.setdefault("llm", {})["model"] = args.model
    if args.max_calls is not None:
        cfg.setdefault("llm", {})["max_calls_per_run"] = args.max_calls
    if args.out:
        cfg["output_dir"] = args.out

    base_out_dir = ROOT / cfg.get("output_dir", "out")
    run_num = 1
    out_dir = base_out_dir / f"run_{run_num}"
    # If --out is not specified, find the next available run number
    if not args.out:
        while out_dir.exists():
            run_num += 1
            out_dir = base_out_dir / f"run_{run_num}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[pipeline] Outputting results to: {out_dir}")

    esc_cfg = cfg.get("escalation", {})
    state_path = ROOT / (args.state_file or esc_cfg.get("state_file", "out/state.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path) 

    # Conditionally filter datasets based on the run schedule in the config.
    schedule_cfg = cfg.get("daily_run_schedule", {})
    force_daily = schedule_cfg.get("force_run_daily_for_testing", False)
    daily_run_hour = schedule_cfg.get("hour_utc", 8)

    now_utc = datetime.now(timezone.utc)
    is_daily_run_time = (now_utc.hour == daily_run_hour)

    all_datasets = cfg["data_source"].get("datasets", [])
    daily_datasets_filtered = [ds for ds in all_datasets if ds["dataset"].endswith("_daily")] # if ds["dataset"].endswith("_daily")
    hourly_datasets = [ds for ds in all_datasets if not ds["dataset"].endswith("_daily")]

    running_daily = False
    if force_daily:
        cfg["data_source"]["datasets"] = daily_datasets_filtered
        running_daily = True
    elif is_daily_run_time:
        cfg["data_source"]["datasets"] = daily_datasets_filtered
        running_daily = True
    else:
        cfg["data_source"]["datasets"] = hourly_datasets

    # For daily runs, we don't need to filter by specific hours.
    # This prevents adding an `hour_stamp` clause to the SQL query for daily tables.
    load_hours = None if running_daily else hours

    date = ''
    hour_range = ''
    all_cases_combined, all_failures_combined = [], []
    
    for entry in load_entries(cfg, date=args.date, hours=load_hours):

        # Process each data source entry independently to avoid cross-contamination of cases.
        spec = load_expectations(EXPECTATIONS_DIR / entry["expectations"], EXPECTATIONS_DIR)   # expectations/osa/amazon.yaml,  expectations 
        failures = run_checks(entry["df"], spec)

        # Fingerprint failures for the current entry only.
        current_cases = fingerprint(failures)

        if not date and not hour_range:
            date = entry.get("date", "").strip()
            # For daily runs, hour_range will be None.
            hour_range = entry.get("hour_range", "").strip() if entry.get("hour_range") else ""
        
        for case in current_cases:
            case["_evidence_cfg"] = entry["evidence"]
            case["total_rows"] = len(entry["df"])
            case["failed_share"] = round(case["failed_count"] / max(len(entry["df"]), 1), 3)
            case["recurrence"] = state["case_streaks"].get(case["case_id"], 0) + 1 
        
        all_failures_combined.extend(failures)
        all_cases_combined.extend(current_cases)

        print(f"[validate] {entry['dataset']}/{entry['platform']}: "
                f"{len(entry['df'])} rows -> {len(failures)} failures -> {len(current_cases)} cases")

    state["case_streaks"] = {c["case_id"]: c["recurrence"] for c in all_cases_combined}

    # ---- ALL cases needing investigation sorted by severity
    to_investigate = sorted(
        [c for c in all_cases_combined if c["on_fail"] == "investigate"], 
        key=severity_rank
    )

    with open(out_dir / "investivate_data.json", "w", encoding="utf-8") as f:
        json.dump(to_investigate, f, indent=2, default=str)

    with open(out_dir / "faliures_data.json", "w", encoding="utf-8") as f:
        json.dump(all_failures_combined, f, indent=2, default=str)

    print(f"\n[pipeline] Total cases to investigate via LLM: {len(to_investigate)}")

    global_repo_assignments = {}
    if to_investigate and not args.no_llm:
        # ---- Sync scraping repo for ALL target combinations (OSA and visibility
        # both need grounded xpaths for the extractor prompt)
        print("Syncing scraping repo and extracting XPaths...")
        repo_cfg = cfg.get("scraping_repo", {})

        unique_targets = {(c["platform"], c["dataset"]) for c in to_investigate}

        for platform, dataset in unique_targets:
            try:
                assignments = repo_handle(
                    platform=platform,
                    table_name=dataset,
                    script_name_map=repo_cfg.get("script_map", {}),
                    repo_url=repo_cfg.get("url"),
                    repo_dir=Path(repo_cfg.get("local_dir", "/tmp/scraping-repo"))
                )
            except Exception as exc:
                print(f"  -> xpath extraction failed for {platform}/{dataset}: {exc}")
                assignments = {}
            global_repo_assignments[f"{platform}|{dataset}"] = assignments

    # ---- Budget guard for this run's LLM calls (see hygiene_check/budget.py
    # for what this does and does NOT cover)
    budget = CallBudget(cfg["llm"].get("max_calls_per_run")) if not args.no_llm else None
    checkpointed = False

    def checkpoint_and_bail(reason: str):
        nonlocal checkpointed
        checkpointed = True
        print(f"\n[pipeline] pausing further LLM calls: {reason}")
        print("[pipeline] work completed so far will still be written to report.json/alert_payload.json")

    llm_providers = cfg["llm"].get("providers", [cfg["llm"]])
    current_provider_index = 0

    # ---- Investigate EVERY case: two-stage per-row/per-case flow
    evidences = []
    for idx, case in enumerate(to_investigate, 1):
        evidence_cfg = case.get("_evidence_cfg") or {}
        case_key = f"{case['platform']}|{case['dataset']}"
        candidate_xpaths = global_repo_assignments.get(case_key, {})

        is_osa = "osa" in case["dataset"].lower()

        try:
            # Always use build_row_evidence to ensure correct, specific HTML is fetched for each row,
            # including OSA cases which require content scanning.
            row_evidence_list = build_row_evidence(evidence_cfg, case)
        except Exception as exc:
            row_evidence_list = [
                {"row_data": row, "evidence": {"html_available": False, "resolve_error": str(exc)}}
                for row in case["sample_rows"][:3]
            ]
        evidences.append(row_evidence_list)

        case_id_clean = case["case_id"].replace("|", "_")
        (out_dir / f"{case_id_clean}.json").write_text(
            json.dumps({"case": case, "evidence_data": row_evidence_list,
                       "llm_config": cfg["llm"]}, indent=2, default=str)
        )

        if args.no_llm or checkpointed:
            case["row_verdicts"] = None
            continue

        n_html = sum(1 for it in row_evidence_list if it["evidence"].get("html_available"))
        print(f"[{idx}/{len(to_investigate)} investigate] {case['case_id']} ")
            #   f"| granularity: {'per-row' if use_per_row else 'per-case'} ")
            #   f"| rows: {len(row_evidence_list)} (html available: {n_html})")

        row_verdicts = []
        for item in row_evidence_list:
            # Add a retry loop to make investigation more resilient to transient errors like timeouts.
            max_retries = cfg.get("llm", {}).get("retries", 2) + 1
            for attempt in range(max_retries):
                try:
                    if current_provider_index >= len(llm_providers):
                        checkpoint_and_bail("all LLM providers exhausted")
                        break
                    
                    provider_cfg = llm_providers[current_provider_index]

                    budget.spend(2)  # one extraction call + one validation call

                    extraction = extract_columns(
                        item["row_data"], item["evidence"], candidate_xpaths, case["dataset"],
                        {**cfg["llm"], **provider_cfg} # Merge provider-specific config
                    )

                    verdict = validate_holistically(
                        case, extraction, {**cfg["llm"], **provider_cfg}
                    )

                    row_verdicts.append({
                        "row_data": item["row_data"],
                        "extraction": extraction,
                        "verdict": verdict.model_dump(),
                        "model": provider_cfg["model"],
                    })
                    print(f"  -> {verdict.cause} (true_positive={verdict.true_positive}, "
                          f"confidence={verdict.confidence:.0%})")
                    break  # Success, exit retry loop

                except BudgetExceeded as exc:
                    checkpoint_and_bail(str(exc))
                    break # Do not retry on budget exceeded
                except Exception as exc:
                    if is_rate_limit_error(exc):
                        print(f"  -> provider failed: {exc}. Switching to next provider.")
                        current_provider_index += 1
                        if current_provider_index >= len(llm_providers):
                            checkpoint_and_bail("all LLM providers failed or were rate-limited")
                            break
                        continue # Retry with the next provider immediately

                    print(f"  -> investigation failed on attempt {attempt + 1}/{max_retries}: {exc}")
                    if attempt + 1 == max_retries:
                        # This was the last attempt, record the failure.
                        row_verdicts.append({
                            "row_data": item["row_data"],
                            "extraction": None,
                            "verdict": {"error": str(exc)},
                            "model": provider_cfg.get("model"),
                        })
                        print(f"  -> investigation failed for one row after {max_retries} attempts: {exc}")

        case["row_verdicts"] = row_verdicts
        true_positives = [rv for rv in row_verdicts
                          if (rv.get("verdict") or {}).get("true_positive")]
        false_positives = [rv for rv in row_verdicts
                           if (rv.get("verdict") or {}).get("true_positive") is False]
        failed_rows = [rv for rv in row_verdicts if "error" in (rv.get("verdict") or {})]
        
        case["true_positive_count"] = len(true_positives)
        case["failed_investigation_count"] = len(failed_rows)

        # Aggregate verdict priority: a confirmed true positive always wins
        # (real problem found, don't bury it). Otherwise, if at least one row
        # was actually investigated and came back false_positive, trust that
        # over an error from a DIFFERENT row in the same case -- one row's
        # LLM call failing must not bury another row's real, checked answer.
        # Only when NO row produced a real verdict do we surface "N failed"
        # explicitly, rather than silently picking whichever row happened to
        # come first — a case where 10/11 rows errored out must not be
        # judged solely on the 1 that parsed, but it also must not be judged
        # as "failed" if that 1 row's answer was legitimate.
        if true_positives:
            case["verdict"] = true_positives[0]["verdict"]
            case["model"] = true_positives[0]["model"]
        elif false_positives:
            case["verdict"] = false_positives[0]["verdict"]
            case["model"] = false_positives[0]["model"]
        elif failed_rows:
            case["verdict"] = {
                "error": f"{len(failed_rows)}/{len(row_verdicts)} row(s) failed "
                         f"investigation (see row_verdicts for each error)",
            }
            case["model"] = failed_rows[0]["model"]
        elif row_verdicts:
            case["verdict"] = row_verdicts[0]["verdict"]
            case["model"] = row_verdicts[0]["model"]
        else:
            case["verdict"] = None
            case["model"] = None

    if checkpointed:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2))
        print(f"[pipeline] state saved to {state_path} — re-run the same command to resume "
              f"remaining cases; already-processed cases keep their verdicts.")

    if evidences:
        with open(out_dir / "row_evidence_list.json", "w", encoding="utf-8") as f:
            json.dump(evidences, f, indent=2, default=str)
    # ---- Escalation Decision: LLM #2's true_positive call is ONE input, not
    # the only gate. Floor/recurrence run unconditionally as a safety net —
    # a large-scale or recurring failure still escalates even if the LLM
    # says false positive, because a validator LLM can be wrong.
    for case in all_cases_combined:
        case["escalation_reasons"] = decide_escalation(case, esc_cfg)
        case.pop("_evidence_cfg", None)

    escalated = [c for c in all_cases_combined if c["escalation_reasons"]]

    report = {
        "cases": all_cases_combined,
        "summary": {
            "total_cases": len(all_cases_combined),
            "investigated": len(to_investigate) if not args.no_llm else 0,
            "escalations": len(escalated),
            "llm_model": cfg["llm"].get("providers", [{}])[0].get("model") if not args.no_llm else None,
            "run_id": run_id,
        }
    }
    
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))

    def alert_entry(c: dict) -> dict:
        verdict = c.get("verdict") or {}
        if verdict.get("error"):
            cause = "investigation_failed"
            summary = verdict["error"]
        else:
            cause = verdict.get("cause", "not_investigated")
            summary = (verdict.get("escalation_summary") or verdict.get("explanation")
                       or "; ".join(c.get("details", [])[:2]))
        return {
            "table_name": f"{c['dataset']}", 
            "platform_name": f"{c['platform']}", 
            "rule_name": f"{c['rule_id']}",
            "column": c.get("column"),
            "severity": c["severity"],
            "escalation_reasons": c["escalation_reasons"],
            "cause": cause,
            "confidence": verdict.get("confidence"),
            "model": c.get("model"),
            "summary": summary,
            "current_xpath": verdict.get("current_xpath"),
            "suggested_xpath": verdict.get("suggested_xpath"),
            "failed_rows": c["failed_count"],
            "true_positive_rows": c.get("true_positive_count"),
            "failed_investigation_rows": c.get("failed_investigation_count"),
            "failed_share": c["failed_share"],
            "recurrence": c["recurrence"],
            "crawls_affected": c["crawls_affected"],
        }

    payload = {
        "alerts": [alert_entry(c) for c in escalated],
        "digest": [{
            "case_id": c["case_id"], 
            "severity": c["severity"],
            "failed_rows": c["failed_count"], 
            "failed_share": c["failed_share"],
            "recurrence": c["recurrence"], 
            "detail": c["details"][0] if c.get("details") else "",
            "cause": (c.get("verdict") or {}).get("cause"),
            "model": c.get("model"),
        } for c in all_cases_combined if not c["escalation_reasons"]],
        "date": date,
        "hour_range": hour_range,
        "llm_model": cfg["llm"].get("providers", [{}])[0].get("model") if not args.no_llm else None,
        "run_id": run_id,
    }
    (out_dir / "alert_payload.json").write_text(json.dumps(payload, indent=2, default=str))
    if not payload["alerts"]:
        print(f"\n[pipeline] no escalations to report for {date} {hour_range} "
              f"(see {out_dir / 'report.json'} for full case details)")
        sys.exit(0)




    email_html = render_email_html(payload=payload)

    BASE_DIR = Path(__file__).resolve().parent
    EMAIL_FILE_PATH = BASE_DIR / "html_body" / "email.html"

    EMAIL_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(EMAIL_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(email_html)

    email_config = cfg.get("email", {})
    recipients = email_config.get("recipients", [])
    if recipients and args.send_email:
        mail.send_mailer(sender='partner', recipient=recipients, body=email_html)
        print(f"Email sent successfully to {recipients}")
    elif recipients:
        print(f"[pipeline] email.html written to {EMAIL_FILE_PATH} but not sent "
              f"(pass --send-email to actually mail {recipients})")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))

    total_cases = report['summary']['total_cases']
    investigated_count = report['summary']['investigated']
    print(f"\n[done] {total_cases} total cases processed ({investigated_count} investigated), "
          f"{len(escalated)} escalations -> {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
