# Phase 1 — DRAM/HBM Manual Research Pipeline

## Goal and scope

Phase 1 accepts one manually supplied DRAM/HBM source at a time as direct text, optionally accompanied by a URL that is stored as metadata only. The only coverage is `dram_hbm` v1.0.0, including its explicit dependency sentinels. There is no automatic URL retrieval or discovery.

## Data flow

`Manual Input → Document → NORMALIZED → DEDUPED → Event → structured Analysis → Thesis Mapping → Signal → Red Team for S2/S3 → Adjudication → local HTML → zero-LLM Ops Check`.

The Harness alone changes database state and enforces source gates. The LLM adapters receive the frozen Coverage/Thesis and raw manual text, return strict Pydantic contracts, and cannot publish, change state, browse, or write the database.

## Configuration

Live runs use `OPENAI_API_KEY`, `OPENAI_MODEL`, optional `OPENAI_RED_TEAM_MODEL`, `LLM_TIMEOUT_SECONDS`, and `LLM_MAX_RETRIES`. Prompts are versioned as `analysis-v1` and `red-team-v1` in `backend/app/prompts/`. Missing credentials are an explicit configuration error with no silent fake fallback.

## Replay

Frozen-output replay reuses saved structured model outputs and must be stable. Live-model replay uses the current configured model, does not promise equality, and saves a field-by-field diff. Original deterministic fixture replay is unchanged.

## Acceptance

Acceptance requires full raw-text/source preservation, strict tier and schema validation, exact evidence references, DRAM/HBM coverage mapping, Red Team for S2/S3, T3 restrictions, numeric sourcing, local brief generation, Ops Check enforcement, both manual replay modes, passing Phase 0 regression tests, and API/CLI operation without real network calls in tests.

## Explicit exclusions

No web fetching, browser automation, search, RSS, News API, Gmail, mail delivery, schedules, additional coverage, frontend, users/auth/multi-tenancy, LangChain/LangGraph/CrewAI, vector database, knowledge graph, portfolio context, Golden Set, prediction, advice, automated trading, Docker, Redis, Celery, cloud deployment, or Phase 2 work is implemented.
