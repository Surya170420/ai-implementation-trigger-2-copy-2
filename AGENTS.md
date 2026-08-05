# AGENTS.md

Guidance for AI agents working in this repository.

## Project overview

This repo hosts **Infytrix AI automation projects**. The shared pip package `ai_toolkit/` provides reusable LLM, structured-output, prompt, observability, and data-validation primitives. The primary application is **`hygiene_check/`** — an automated pipeline that validates e-commerce scraper data (visibility and OSA datasets) from Amazon, Flipkart, and other platforms, investigates failures with an LLM, and produces alert payloads for email escalation.

**Core flow:** validate → fingerprint → investigate → report → alert.

## Repository layout

```
ai_toolkit/                    # Shared pip-installable package (infytrix-ai-toolkit)
  llm.py                       # generate() — any provider via litellm/ollama SDK
  structured.py                # Schema-validated LLM output (pydantic) with retry
  prompts.py                   # Prompt templates with {{ variable }} slots
  observability.py             # JSONL log of every LLM call (llm_calls.jsonl)
  config.py                    # YAML config loading with env var substitution
  verify.py                    # extract/validate helpers for two-stage LLM flow
  checks/
    engine.py                  # Load expectations YAML, run rules, fingerprint failures
    library.py                 # Check implementations (not_null, regex, share_max, …)
  mcp_servers/                 # MCP mail/websearch servers

hygiene_check/                 # Scraper data validation project
  pipeline.py                  # Main entry point — orchestrates the full run
  investigator.py              # Two-stage LLM verdict (extract_columns → validate_holistically)
  evidence.py                  # Resolve raw crawl HTML from MinIO/local zips
  data_sources.py              # Trino (prod) or CSV fixtures (local dev)
  budget.py                    # Soft cap on LLM calls per run + rate-limit handling
  trigger.py                   # 4-hour scheduler loop for production
  extractors/script_extractor.py  # Clone scraping-repo, AST-parse XPath assignments
  expectations/                # Rule files (config, not code)
    visibility/                # base.yaml + amazon.yaml + flipkart.yaml
    osa/                       # base.yaml + amazon.yaml + flipkart.yaml
  prompts/                     # investigator.md, extractor.md, validator.md
  html_body/                   # Email rendering (html_render.py, email.html)
  config.yaml                  # Production config (Trino + Ollama Cloud)
  config.local.yaml            # Local dev config (CSV fixtures + localhost Ollama)
  config.cloud.yaml            # Cloud-specific overrides

out/                           # Pipeline outputs (run_1/, run_2/, …)
tmp/scraping-repo/             # Cloned scraping-repo for XPath extraction (gitignored clone)
sample/                        # Test fixtures: CSVs + raw HTML zips (when present)
```

## Architecture

```text
Trino/CSV  →  run_checks()  →  fingerprint()  →  build_row_evidence()
                                                      ↓
                                              extract_columns()  (LLM #1)
                                                      ↓
                                              validate_holistically()  (LLM #2)
                                                      ↓
                                              decide_escalation()  →  report.json / alert_payload.json
```

### Design principles (do not violate)

1. **Deterministic rules gate everything.** The LLM only sees fingerprinted anomaly cases — never clean rows. One broken XPath at 50k rows = one case, not thousands of LLM calls.
2. **No automatic provider fallback.** Each project picks its model/endpoint in config. If fallback is needed, implement it explicitly around `generate()`.
3. **Null values pass every row check except `not_null`/`constant`.** "Must exist" and "must look right when present" are separate rules.
4. **Expectations are config, not code.** Add or change validation rules in `hygiene_check/expectations/**/*.yaml`, not in Python, unless introducing a new check type.
5. **Platform files extend base files.** A child rule with the same `id` replaces the base rule. `{{ platform }}` in params is substituted from the file's `platform:` key.
6. **Budget checkpointing is intentional.** When `max_calls_per_run` is hit or a rate-limit error occurs, the pipeline writes partial results and exits cleanly (exit 0). Re-running resumes; do not treat this as a failure.

### Escalation policy

A case reaches `alert_payload.json` (and the email) when `decide_escalation()` in `pipeline.py` returns reasons:

- **`ai_verdict`** — LLM says `true_positive: true` at or above `min_confidence` (default 0.5).
- **`low_confidence`** — LLM confidence below 0.6, regardless of verdict.

`failed_share` and `recurrence` are computed and logged in `report.json` but are **not** current escalation triggers.

## Running the pipeline

From the repo root:

```bash
pip install -e .                                              # install ai_toolkit + deps
python -m hygiene_check.pipeline --no-llm                       # rules + fingerprinting only
python -m hygiene_check.pipeline                                # full run (needs LLM endpoint)
python -m hygiene_check.pipeline --config hygiene_check/config.local.yaml
python -m hygiene_check.pipeline --date 2026-07-09 --hours 6,7,8,9
python -m hygiene_check.pipeline --send-email                 # actually send alert email
python -m hygiene_check.trigger                                 # 4-hour scheduler loop
```

### Outputs (per run in `out/run_N/`)

| File | Purpose |
|------|---------|
| `report.json` | All cases with counts, samples, row_verdicts |
| `alert_payload.json` | Escalations only — feed to org mailer |
| `<case_id>.json` | Per-case evidence artifact (written before LLM calls) |
| `investivate_data.json` | Cases queued for investigation |
| `faliures_data.json` | Raw failure records |
| `row_evidence_list.json` | HTML evidence bundles per case |

State for recurrence tracking: `out/state.json`.

## Environment variables

| Variable | Used for |
|----------|----------|
| `OLLAMA_API_KEY` | Ollama Cloud auth (production config) |
| `SCRAPING_REPO_TOKEN` | GitHub PAT for cloning `scraping-repo` |
| `.env` in `hygiene_check/` | Loaded via `python-dotenv` at runtime |

Never hardcode tokens or API keys in YAML or Python. Use `${VAR}` substitution in config (see `ai_toolkit/config.py`).

## Common tasks

### Add a validation rule

1. Edit or extend the appropriate expectations file under `hygiene_check/expectations/`.
2. Use an existing check from `ai_toolkit/checks/library.py` when possible.
3. Set `on_fail: investigate` only when the LLM should examine HTML evidence; use `report_only` for cheap sanity checks.
4. Add a `description:` — it flows into the investigator prompt and improves verdict quality.

Rule shape:

```yaml
- id: unique_rule_name
  column: column_name
  check: not_null          # from ai_toolkit/checks/library.py
  params: {}
  scope: row               # row | crawl | run
  when: "optional pandas query guard"
  severity: error          # error | warn
  on_fail: investigate     # investigate | report_only
  description: >
    Why this rule exists — domain context for the LLM investigator.
```

### Add a new check type

1. Implement the function in `ai_toolkit/checks/library.py` with `@register("name", kind="row"|"group")`.
2. Reference it by name in expectations YAML.

### Change LLM behavior

- Prompts: `hygiene_check/prompts/extractor.md`, `validator.md`, `investigator.md`
- Two-stage flow: `hygiene_check/investigator.py` (`extract_columns`, `validate_holistically`)
- Model/endpoint: `llm:` section in the active config file
- Call budget: `llm.max_calls_per_run` in config or `--max-calls` CLI flag

### Change data sources

- **Local dev:** set `data_source.mode: csv_fixtures` in `config.local.yaml`
- **Production:** set `data_source.mode: trino` — requires cluster-internal Trino access
- Trino queries must use `is_select=True` via `common_utils_repository.trino.trino_query`

### Change HTML evidence resolution

- Logic lives in `hygiene_check/evidence.py`
- MinIO partition template is in `config.yaml` under each dataset's `evidence:` block
- OSA filenames carry only the first 5 product codes per batch; unmatched codes fall back to scanning up to 20 partition zips

### Change email output

- Template rendering: `hygiene_check/html_body/html_render.py`
- Output file: `hygiene_check/html_body/email.html`
- Sending: `--send-email` flag uses `common_utils_repository.mailer`

## Code conventions

- **Python 3.10+**, type hints on public functions, `from __future__ import annotations` in modules.
- **Minimal diffs.** Match existing naming, import style, and documentation level. Do not refactor unrelated code.
- **Comments** only for non-obvious business logic (escalation policy tradeoffs, budget behavior, HTML matching quirks).
- **Do not commit** `.env`, tokens, or `out/` run artifacts unless explicitly requested.
- **Do not modify** `tmp/scraping-repo/` — it is a cloned dependency synced at runtime.
- **Do not add automatic LLM provider fallback** in `ai_toolkit/llm.py` without explicit project-level handling.

## Key files to read first

When working on a task, start with the file that owns the behavior:

| Task | Start here |
|------|------------|
| Pipeline orchestration | `hygiene_check/pipeline.py` |
| Rule engine / fingerprinting | `ai_toolkit/checks/engine.py` |
| Check implementations | `ai_toolkit/checks/library.py` |
| LLM calls | `ai_toolkit/llm.py`, `ai_toolkit/structured.py` |
| Investigation logic | `hygiene_check/investigator.py` |
| HTML evidence | `hygiene_check/evidence.py` |
| Data loading | `hygiene_check/data_sources.py` |
| XPath extraction | `hygiene_check/extractors/script_extractor.py` |
| Config | `hygiene_check/config.yaml`, `ai_toolkit/config.py` |

## Testing

- `--no-llm` is the primary fast test path — validates rules and fingerprinting without LLM cost.
- Use `config.local.yaml` with CSV fixtures for offline development.
- `hygiene_check/test.py` and `hygiene_check/model_bench.py` exist for ad-hoc testing/benchmarking.
- Benchmark runs: use `--out`, `--state-file`, and `--max-calls` to isolate runs; never pass `--send-email` from benchmarks.

## External dependencies

- **`common-utils-repository`** (git dep): Trino queries, MinIO access, Outlook mailer
- **`scraping-repo`** (cloned at runtime): Crawler scripts for XPath extraction
- **Trino cluster** (`trino-cluster-02.infytrix.in`): Production data — only reachable from in-cluster/Airflow workers
- **MinIO/lakehouse**: Bronze-layer raw HTML zips for evidence

## Gotchas

- `fingerprint()` populates `case["sample_rows"]`, not `case["samples"]`. Evidence builders must use `sample_rows`.
- Each pipeline run creates a new `out/run_N/` directory (auto-incremented unless `--out` is specified).
- Two LLM calls per investigated row: `extract_columns` + `validate_holistically`. Budget `spend(2)` reflects this.
- `VISIBILITY_PER_ROW_CAP = 50` in `pipeline.py` controls per-row vs case-level evidence granularity.
- README escalation docs (floor/recurrence triggers) are **outdated** — trust `decide_escalation()` in `pipeline.py` and the `escalation:` comments in `config.yaml`.
- Production Trino host is cluster-internal; local runs against Trino will fail unless on VPN/in-cluster.
