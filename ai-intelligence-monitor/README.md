# AI Intelligence Monitor — Phase 0–2

An auditable harness for dependency-based personal investment research. Phase 0 provides versioned coverage, evidence contracts, a controlled state machine, replay, deterministic operations checks, and fixture-driven tests. Phase 1 adds manually supplied DRAM/HBM source text and bounded OpenAI or DeepSeek structured analysis. Phase 2 adds manual, allowlisted collection from three official T1 sources. It still does not search the web, run a browser, schedule jobs, connect Gmail, send mail, provide investment advice, or expose a frontend.

## Quick start

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
alembic upgrade head
pytest -q
python -m app.scripts.seed_demo
python -m app.scripts.run_demo
python -m app.scripts.ops_check --latest
python -m app.scripts.replay_run --run-id <RUN_ID>
uvicorn app.main:app --reload
```

The CLI uses `backend/data/monitor.db`, `config/coverage/dram_hbm.yaml`, `config/sources/official_sources.yaml`, `fixtures/`, `fetches/`, and `runs/` by default. All timestamps are UTC. SQLite foreign keys are enabled on every connection.

## Minimal API

- `GET /health`
- `GET /api/coverage`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/signals`
- `GET /api/fixed-sources`
- `POST /api/fixed-sources/{source_key}/fetch`
- `GET /api/fetch-audits`
- `POST /api/demo/run`
- `POST /api/runs/{run_id}/replay`

Standards and scope are normative in [`docs/`](docs/).

## Phase 1 configuration

Copy the example into the backend working directory and provide the API key through a local ignored file or environment variable:

```bash
cp ../.env.example .env
```

`LLM_PROVIDER` selects `openai` (the default) or `deepseek`. OpenAI uses `OPENAI_API_KEY`, `OPENAI_MODEL`, and optional `OPENAI_RED_TEAM_MODEL`. DeepSeek uses `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, and optional `DEEPSEEK_RED_TEAM_MODEL`. DeepSeek calls its OpenAI-compatible Chat Completions endpoint with JSON output, then validates `json.loads` output through the same Pydantic and evidence contracts. `LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` apply to either provider. Missing credentials produce an explicit configuration error; the system never falls back silently to a fake model.

To select DeepSeek in `backend/.env`:

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_RED_TEAM_MODEL=deepseek-v4-pro
```

Keep the key value local and uncommitted. Selecting DeepSeek does not remove or modify the OpenAI configuration.

## Manual API

The URL is metadata only; the server never fetches it.

```bash
curl -X POST http://127.0.0.1:8000/api/manual-documents \
  -H 'content-type: application/json' \
  -d '{
    "title":"Micron HBM update",
    "url":"https://example.com/source",
    "source_name":"Micron IR",
    "source_tier":"T1",
    "published_at":"2026-07-17T00:00:00Z",
    "language":"en",
    "raw_text":"Paste the complete source text here; it must contain at least forty characters.",
    "optional_notes":"URL is retained as source metadata."
  }'

curl -X POST http://127.0.0.1:8000/api/manual-documents/<DOCUMENT_ID>/analyze
curl -X POST 'http://127.0.0.1:8000/api/runs/<RUN_ID>/replay?mode=frozen-output'
curl -X POST 'http://127.0.0.1:8000/api/runs/<RUN_ID>/replay?mode=live-model'
```

## Manual CLI

```bash
python -m app.scripts.ingest_manual \
  --title "Micron HBM update" \
  --source-name "Micron IR" \
  --source-tier T1 \
  --published-at "2026-07-17T00:00:00Z" \
  --url "https://example.com/source" \
  --file /path/to/article.txt \
  --analyze
```

Without `--analyze`, the command only stores the manual document.

## Phase 2 controlled source collection

The versioned registry is `config/sources/dram_hbm_sources.yaml`. Enabled sources are Micron IR, Samsung Global Newsroom Semiconductors, and NVIDIA Press Release RSS. SK hynix is registered but disabled because its official page previously returned 403. Collection is always manually triggered and accepts only registered source IDs—never arbitrary URLs.

Default behavior is discovery, deterministic relevance prefilter, official article fetch, extraction, deduplication, and Document ingestion. It does not call an LLM unless `--analyze` is explicitly supplied. `--dry-run` performs discovery, prefilter, and URL validation without fetching articles, saving Documents, or invoking DeepSeek.

```bash
python -m app.scripts.collect_sources \
  --coverage dram_hbm \
  --source micron_ir_latest_news \
  --source samsung_semiconductor_newsroom \
  --source nvidia_press_release_rss \
  --since-hours 168 \
  --max-items-per-source 5 \
  --dry-run

python -m app.scripts.collect_sources \
  --coverage dram_hbm \
  --source micron_ir_latest_news \
  --max-items-per-source 3

curl -X POST http://127.0.0.1:8000/api/source-collections \
  -H 'content-type: application/json' \
  -d '{"coverage_id":"dram_hbm","source_ids":["micron_ir_latest_news"],"since_hours":168,"max_items_per_source":3,"analyze":false,"dry_run":false}'
```

Each run writes `runs/<run_id>/00-source-fetch.json`, including discoveries, prefilter decisions, fetch outcomes, duplicates, failures, budgets, registry/Coverage snapshots, and Source Ops results. HTTPS/domain allowlists apply before every request and after every redirect. Local/private addresses, external redirects, browser automation, 403 bypass, proxies, cookies, attachments, and article-link expansion are prohibited. A fetched document stays at `COLLECTED` unless analysis was explicitly requested.

```bash
curl -X POST http://127.0.0.1:8000/api/manual-documents/<DOCUMENT_ID>/analyze
```

At most 10 Documents and 3 analyses are allowed per run. Re-fetching the same canonical URL or normalized content reuses the existing Document and never calls the LLM again. Frozen-source replay uses only the saved local snapshots and never accesses the network.
