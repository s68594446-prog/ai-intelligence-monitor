# Phase 3B — Bounded Multi-provider Web Discovery

Status: **PASS / live verified**. The local SearXNG instance, hosted providers, bounded live Discovery, Discovery Ops, and frozen-search replay have passed. The acceptance run produced no safely fetchable source text because the local DNS layer returned reserved benchmark addresses for every selected T1 domain; the unchanged SSRF Gate correctly failed closed, so no Document or Analysis was created. Phase 3B does not change Analysis, Red Team, Signal, Adjudication, Evidence, Ops, Replay, or Phase 3A email rules.

## Explicit execution and provider routing

The default command is offline. It writes the versioned plan without network or model access:

```bash
cd backend
python -m app.scripts.discover_web
```

External stages remain explicit and ordered: `--search`, `--triage`, `--fetch`, then `--analyze`. `WEB_DISCOVERY_ENABLED=false` remains the safe default. The provider registry accepts only `searxng`, `tavily`, `exa`, `you`, `gemini_google`, and `brave`; endpoints cannot be supplied through the CLI.

Normal routing calls one configured primary provider. A retryable network, timeout, quota/rate, or upstream 5xx failure may invoke at most one configured fallback for that query. Authentication or authorization failures and zero-result responses do not trigger fallback. Every attempt is audited against the shared query and request budgets.

TheNewsAPI is a separate, optional `NEWS_DISCOVERY` supplement, not a Web fallback. It is disabled by default, accepts only the fixed `thenewsapi` provider, reuses at most two deterministic Coverage queries, requests at most three recent articles per query and page one only, and has its own recorded budget inside the overall Discovery cap. Enable it explicitly with `--news --news-provider thenewsapi`; omit `--news` to disable it. A combined run may use both `--search` and `--news` before the common `--triage --fetch --analyze` stages.

TheNewsAPI's free plan has limited requests and article counts. Its UUID, description, snippet, keywords, categories, and source label are provider provenance or discovery metadata only. The provider does not supply authorized Document text: the article URL must still pass Source Policy, SSRF checks, and safe fetch before its original text can become Evidence. HTTP 402 means the plan usage allowance is exhausted; HTTP 429 means the short-term rate limit was reached. Neither error falls back to a general Web provider or retries automatically. Usage-header presence and whether remaining quota was parsed are audited without printing the key or complete response.

Provider snippets, descriptions, scores, and generated text are `DISCOVERY_ONLY`. They never become Evidence, Source Tier, Claims, Signals, Adjudication, or alerts. Only safely fetched T1/T2 source text may enter a Document and the unchanged Analysis Harness. Gemini is a Search provider only: generated answers are discarded and only grounding citation metadata is retained. Public search queries sent to Gemini must contain no thesis, document, run, personal, or project data; provider-side retention may apply.

The configured Gemini default is the stable `gemini-2.5-flash`, selected after the current project returned `NOT_FOUND` for Flash-Lite while the stable Flash grounding request succeeded. Operators may change `GEMINI_SEARCH_MODEL` only to a documented model identifier that supports Google Search grounding; the API base and operation remain fixed.

The audited local runtime currently pins official SearXNG `2026.7.19-6da6eee26` by RepoDigest. The runtime-only compose configuration binds the host port to `127.0.0.1:8080`, gives SearXNG a fixed private container address rather than an all-interface application bind, and uses `restart: unless-stopped`. Docker Desktop start-at-login remains an operator choice and is not enabled by the project.

## Safety boundaries

- Source Tier is assigned by `web-source-policy-v1`, never by a provider or LLM. Unknown domains remain `UNVERIFIED`; T3 and unverified results are discovery-only.
- Every result URL still passes canonicalization, HTTPS, public DNS, redirect-by-redirect DNS/IP, content type, compressed/decompressed size, and access-challenge checks before fetch. The localhost exception applies only to the exact SearXNG API endpoint and never to result URLs.
- Search provider responses are size-limited, reduced to an audited allowlist, and stored with a SHA-256 digest. Same-URL results merge query and provider provenance. At most one eligible Document per event cluster enters Analysis, preferring T1.
- Frozen-search replay consumes only saved snapshots and makes zero network, Search provider, or LLM calls.
- Discovery never calls SMTP. Any later email remains controlled by Phase 3A and `EMAIL_AUTO_SEND=false` by default.

## Local SearXNG runtime

SearXNG is an operations dependency, not project source. Its runtime directory is:

`/Users/shh/Library/Application Support/ai-intelligence-monitor/searxng`

It must remain outside `/Users/shh/Documents/0717`, must never be staged, and must contain its locally generated server secret only. Do not copy the runtime compose file, settings, logs, or secret into this repository.

Prerequisites are Docker Desktop with a working Docker daemon and an unused TCP port 8080. The compose configuration must use the official SearXNG image, preferably pinned to the recorded repository digest, enable only HTML and JSON response formats, and publish exactly `127.0.0.1:8080:8080`. It must not use `0.0.0.0`, a public domain, Caddy, or an unnecessary Valkey service.

From the runtime directory, the normal operations are:

```bash
docker compose config
docker compose up -d
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8080/
curl --fail --silent --show-error 'http://127.0.0.1:8080/search?q=HBM&format=json'
docker compose logs --tail 100 searxng
docker compose stop
```

Do not paste the JSON response into reports because it contains search text. Confirm loopback-only publication with `docker compose port searxng 8080` and the host socket inspection tool; the published address must be `127.0.0.1`, not all interfaces.

For an update, first record the running image ID and `RepoDigests`, pull the intended official image, inspect its new digest, deliberately update the runtime-only digest pin, validate with `docker compose config`, and recreate only this compose project. Never delete unrelated containers, images, networks, or volumes.

Rotate the SearXNG server secret by generating a new cryptographically random value locally, replacing only the runtime settings value without printing it, validating the compose configuration, and recreating this service. Keep the runtime file permission-restricted. If port 8080 is occupied, stop and identify the owner; do not kill it or expose SearXNG on another interface automatically.

To disable SearXNG while retaining its configuration, run `docker compose stop` in the runtime directory and explicitly select a hosted provider for a run. Safe provider selection examples are:

```bash
python -m app.scripts.discover_web --provider tavily --search
python -m app.scripts.discover_web --provider exa --search
python -m app.scripts.discover_web --provider you --search
python -m app.scripts.discover_web --provider gemini_google --search
python -m app.scripts.discover_web --provider searxng --search
python -m app.scripts.discover_web --news --news-provider thenewsapi
```

Restore SearXNG as the configured default only after its health and JSON checks pass. To uninstall later, first stop the compose project and inventory its runtime files and Docker resources; removal is an explicit operator action. Never use `rm -rf`, and do not perform uninstall as part of application deployment.

## Artifacts and exclusions

Every run writes `00-search-plan.json` through `04-discovery-decisions.json`. Artifacts retain provider/version, request ID when supplied, query hash, raw-response digest, routing attempts, provenance, normalized results, triage, fetch decisions, event clustering, Document decisions, and Ops results. Runtime `runs/`, `fetches/`, the SQLite database, local environment files, and the external SearXNG runtime are excluded from version control.

No scheduler, queue, frontend, extra Coverage, translation, PDF/OCR, Gmail/Outlook integration, retry worker, public SearXNG service, news scheduler, or automatic trading behavior is included.
