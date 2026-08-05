# main-repo — AI automation projects

One repo: `ai_toolkit/` is the shared pip-installable package; each project is
a sibling folder that imports it.

```
ai_toolkit/                shared AI components (the pip package)
    llm.py                 generate(prompt, model=..., base_url=...) — any provider via litellm
    structured.py          schema-validated LLM output (pydantic) with retry
    prompts.py             prompt templates as files with {{ variable }} slots
    observability.py       JSONL log of every LLM call
    config.py              YAML config loading
    checks/
        engine.py          loads expectations YAML, runs rules, fingerprints failures
        library.py         check implementations (not_null, regex, share_max, ...)

hygiene_check/             project: scraper data validation (visi / osa)
    expectations/          the rule files — config, not code
        visibility/        base.yaml + amazon.yaml + flipkart.yaml (extends base)
        osa/               base.yaml + amazon.yaml
    pipeline.py            validate -> fingerprint -> investigate -> report
    investigator.py        AI verdict (cause, confidence, suggested_xpath, escalate)
    html_evidence.py       finds raw crawl HTML, slices the relevant fragment
    prompts/investigator.md
    config.yaml            runtime parameters (model, endpoint, data source, caps)

sample/                    test fixtures: real CSVs + raw HTML zips
```

## Quickstart

```bash
pip install -e .                          # installs ai_toolkit + dependencies
python -m hygiene_check.pipeline --no-llm # rules + grouping only
python -m hygiene_check.pipeline          # full run (needs an Ollama endpoint)
```

Outputs land in `out/`: `report.json` (all cases) and `alert_payload.json`
(`alerts` for the org mailer + `digest` of everything else).

## Escalation policy

Three independent triggers; any one puts a case in `alerts` (see
`escalation:` in config.yaml):

1. **AI verdict** — the investigator concluded a human must act.
2. **Floor** — severity `error` and failed share above `force_escalate_above`
   (default 0.5; per-rule override via `force_escalate_above` on the rule).
   The AI explains the cause but cannot silence a large failure.
3. **Recurrence** — same case fingerprint in `recur_after` consecutive runs
   (streaks kept in `state_file`). "Transient" is only a valid excuse once.
   Recurrence never promotes warn+report_only rules — those are deliberately
   kept out of the alert path and stay in the digest.

## Rule anatomy

Every rule in every expectations file has the same shape:

```yaml
- id: image_diversity            # unique name -> reports & case fingerprints
  column: product_image_url
  check: share_max               # from ai_toolkit/checks/library.py
  params: {max_share: 0.5}
  scope: crawl                   # row | crawl | run
  when: "stock_status == 'In Stock'"   # optional guard
  severity: error                # error | warn
  on_fail: investigate           # investigate (AI) | report_only
  description: >                 # optional but powerful: WHY the rule exists.
    Flows into the AI investigator's prompt as domain knowledge and
    measurably improves verdict quality.
```

Platform files `extends:` a base file; a child rule with the same `id`
replaces the base one. `{{ platform }}` in params is substituted from the
file's `platform:`.

Design principles:
- AI never sees clean rows — deterministic rules gate everything, the LLM
  only investigates fingerprinted anomaly cases (one broken xpath at 50k rows
  = one case, not thousands).
- No automatic provider fallback — each project picks its model/endpoint in
  config.yaml; fallback, if wanted, is the project's own exception handling.
- Null values pass every row check except not_null/constant, so "must exist"
  and "must look right when present" are separate rules.

## Production wiring (hygiene-check)

Both connectors are implemented via `common_utils_repository`:

- **Trino** (`data_sources.py`): `data_source.mode: trino` queries the silver
  tables per dataset+platform, windowed to the last `lookback_hours` (or an
  explicit `--date 2026-07-09 --hours 6,7,8,9` for backfills/testing).
  Note: `trino_query` must be called with `is_select=True` for SELECTs.
- **MinIO raw HTML** (`html_evidence.py`): lists the bronze partition
  `layer=bronze/blobstorage/table_name=<t>/platform_id=<id>/date_stamp=<d>/hour_stamp=<h>/`,
  matches zips by normalized keyword ('+airwrap +hair' style names) or
  product code; OSA filenames only carry the first 5 codes of a batch, so
  unmatched codes fall back to scanning inside up to 20 partition zips.
- **LLM**: set `llm.base_url` to the Ollama k8s DNS in prod.
- **Alerts**: `out/alert_payload.json` (`alerts` + `digest`) is the mailer input.

The Trino host is cluster-internal — run the pipeline from a machine that
reaches it (Airflow worker / in-cluster pod):

```bash
python -m hygiene_check.pipeline                      # last 4 hours
un run -m hygiene_check.trigger
python -m hygiene_check.pipeline --date 2026-07-09 --hours 6,7,8,9
```
