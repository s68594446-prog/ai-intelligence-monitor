# AI Intelligence Monitor — Phase 0–3C

An auditable harness for dependency-based personal investment research. Phase 0 provides versioned coverage, evidence contracts, a controlled state machine, replay, deterministic operations checks, and fixture-driven tests. Phase 1 adds manually supplied DRAM/HBM source text and bounded OpenAI or DeepSeek structured analysis. Phase 2 adds manual, allowlisted collection from three official T1 sources. Phase 3A adds disabled-by-default, gated SMTP email delivery for an existing passed alert brief. Phase 3B adds disabled-by-default, bounded multi-provider web discovery while preserving the existing evidence and signal gates. Phase 3C is an isolated GitHub Actions daily AI-supply-chain digest with its own deterministic Digest Ops and email gate. The frozen DRAM/HBM Harness itself remains unscheduled and unchanged. The project does not connect Gmail, provide investment advice, or expose a frontend.

The additive daily China AI channel is isolated from the frozen DRAM/HBM Signal pipeline. TianAPI is optional and disabled by default. Juhe ID 235 technology headlines and ID 850 AI briefs are separately keyed, bounded discovery providers; their titles and summaries are never evidence. A-share scope is anchored to official constituents of CSI `931071` and SSE STAR `950180`; final items require safely fetched official primary text and exact evidence quotes. Juhe stock data ID 21 is not integrated. See [`docs/DAILY_AI_A_SHARE.md`](docs/DAILY_AI_A_SHARE.md).

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

## Phase 3A controlled email delivery

Email is disabled by default. The delivery service only accepts an existing, non-replay manual Analysis Run that passed Ops Check, contains an `OPS_CHECKED` S2/S3 `SEND_ALERT` Signal with Red Team output, and has its audited `05-brief.html` marked `PASSED`. SMTP supports only STARTTLS or SSL. Credentials belong only in the ignored `backend/.env`; recipient addresses are stored only as a SHA-256 fingerprint.

The default CLI performs a local Gate check and never connects to SMTP:

```bash
python -m app.scripts.deliver_email --run-id <RUN_ID>
```

`--send` is the only path that permits SMTP delivery and must not be used without a separately approved real-email acceptance run. `EMAIL_AUTO_SEND=false` is the default. Phase 3A includes no scheduler, retry queue, Gmail/Outlook integration, attachments, tracking, or frontend. See [`docs/PHASE_3A.md`](docs/PHASE_3A.md) for Gate, idempotency, security, and SMTP limitations.

## Phase 3B bounded web discovery

Phase 3B is **PASS / live verified**. It adds a disabled-by-default provider registry for a private loopback SearXNG instance plus Tavily, Exa, You.com, Gemini Google Search grounding, and Brave. A separately disabled TheNewsAPI channel can add a small, recent news-discovery sample but is never part of the Web fallback chain. It retains the deterministic query plan, domain-based Source Policy, constrained Discovery Triage, safe public-web text fetching, provenance, deduplication, Ops Check, and frozen-search replay. Provider snippets and generated text are discovery metadata only; they never become Evidence. It does not change the existing Analysis or email gates.

The default command is offline and only writes/displays the versioned query plan:

```bash
python -m app.scripts.discover_web
```

Network and model stages require explicit ordered flags: `--search --triage --fetch --analyze`. The provider is selected from a fixed registry with `--provider`; arbitrary endpoints are not accepted. The default SearXNG endpoint is fixed to `http://127.0.0.1:8080/search`, and all returned URLs still pass the public-web fetch gates. See [`docs/PHASE_3B.md`](docs/PHASE_3B.md) for routing and local runtime operations.

News discovery is invoked only with `--news --news-provider thenewsapi` and remains bounded to page one, two deterministic queries, and three links per query. The free allowance may return 402 (usage exhausted) or 429 (rate limited); both fail closed without switching to a Web provider. The API supplies only short metadata and links. Formal Evidence still requires a successful safe fetch of the original T1/T2 article. Usage headers can be audited through the run artifact without displaying the credential or complete provider response.

## Phase 3C scheduled daily supply-chain digest

Phase 3C is additive and does not write the frozen DRAM/HBM Run, Signal, Adjudication, Evidence, Replay, or Ops state. The parent repository workflow `.github/workflows/daily-digest.yml` runs at `22:30 UTC` (nominally `06:30 Asia/Shanghai`; GitHub schedules may start later), performs bounded RSS/Juhe discovery, safe T1/T2 fetch, Gemini structured analysis, deterministic selection, rendering, and Digest Ops, then uploads the complete run artifact.

Phase 3C reads `GEMINI_API_KEY`, `GEMINI_ANALYSIS_MODEL`, and `GEMINI_BASE_URL`. The primary model defaults to `gemini-3.5-flash`. An operator may explicitly configure the lower `gemini-3.5-flash-lite` with `GEMINI_FALLBACK_MODEL` and enable a one-time daily-quota transition with `GEMINI_QUOTA_FALLBACK_ENABLED=true`. The free tier is quota-limited, is not described or treated as unlimited, and no paid or DeepSeek fallback occurs. The actual model chain and transition reason are audited and displayed in the daily email. The v9 system instruction is `backend/app/prompts/daily_global_ai_analysis_v9.txt`; department prompts only narrow scope and cannot weaken the shared Evidence, importance, neutrality, or market-action rules. Requests contain only the safely fetched source text, URL, and bounded metadata; Search Grounding and all other Gemini tools are disabled. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for department contracts, immutable handoffs, replay, and Delivery Gate boundaries.

Transient Gemini 429/5xx responses receive bounded retries with short capped backoff while the configured per-run HTTP request budget remains authoritative. A per-minute 429 receives one bounded cooldown. An explicit per-model daily-quota response may switch once to the configured lower model; missing/disabled fallback, authentication failure, fallback exhaustion, or incomplete coverage still fails closed. Safely fetched sources are compared with the committed three-day `digests/.source-history.jsonl`; Gemini is skipped only when both canonical URL and content SHA-256 match, while changed content at the same URL remains eligible. The history contains no raw text.

Digest publication and email are fail-closed:

The scheduled run allows at most 50 successful fetch candidates and 65 total
Gemini HTTP requests, including retries and the explicit model transition.
`--max-provider-requests` sets that request ceiling (hard maximum 80); without
it, the legacy `--max-analyze` request ceiling is preserved. All fetched,
non-duplicate candidates remain in the coverage denominator. No candidate is
dropped merely to make Ops pass. Timeout recovery uses the existing bounded
retry count and shared 2400-second deadline. Recovered HTTP failures and
validated responses are audited in `provider-request-events.json`; unresolved
candidate failures remain in `analysis-failures.json` and continue to block
delivery when required by the department Gate.

- A source-tier/model-label mismatch is rejected per item as `SOURCING_TIER_MISMATCH` and retained in `analysis-failures.json`; one rejected unpublished analysis cannot block otherwise valid items.
- If fetched sources produced analysis attempts but Gemini returned zero schema-validated outputs, `analysis_provider_usable` fails Digest Ops; this cannot be reported as an honest empty-news day.
- Digest Ops rechecks source-tier consistency and records named offenders as a backstop.
- A missing or failed `ops.json` makes the GitHub job fail after artifact upload. No digest is committed and no email is attempted.
- Only an Ops-passed digest is staged, email-gated, sent, and then committed together with its email ledger update.
- Any email Gate block or SMTP failure makes the workflow fail instead of being reported as a green run.

The day-level ledger guard prevents ordinary scheduled duplicates, and the delivery service also uses a day/content/recipient idempotency key. Manual dispatch of `daily-digest.yml` defaults to a complete discovery/fetch/Gemini/render/Ops dry-run with `send_email=false`; it does not contact SMTP, append the send ledger, or commit generated files. Scheduled runs continue to send, and a manual send requires explicit `send_email=true`. The separate manual `email-digest.yml` workflow remains available for Gate dry-runs or controlled delivery attempts against an already committed digest. SMTP and provider credentials remain GitHub Secrets or local ignored environment values and are never written to digest artifacts. Successful send-ledger rows retain only a deterministic Message-ID, sender domain, acceptance timestamp, idempotency key, and recipient count for delivery troubleshooting; recipient addresses are not stored.

`.github/workflows/backend-ci.yml` runs the complete offline backend test suite on relevant pushes and pull requests with provider credentials forced empty. It never performs discovery, Gemini calls, or email delivery.

The HTML alternative is a self-contained, table-based 660px intelligence brief with inline CSS and clickable source links. It contains no external images, fonts, scripts, remote CSS, tracking pixels, or audit appendix; the original plain-text alternative is unchanged.
