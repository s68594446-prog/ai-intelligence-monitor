# Phase 2 — Controlled Fixed-Source Collection

## Goal and scope

Phase 2 manually collects DRAM/HBM intelligence only from a versioned allowlist of official T1 sources. It is not general web search and does not add Coverage, scheduling, browser automation, or autonomous analysis.

## Source Registry

`config/sources/dram_hbm_sources.yaml` records registry version, `dram_hbm` coverage, source role, RSS/HTML discovery method, official discovery URL, allowed domains and article patterns, language, conservative request limits, deterministic relevance/exclusion terms, and notes. Micron IR, Samsung Semiconductor Newsroom, and NVIDIA Press Release RSS are enabled. SK hynix remains disabled; 403 must not be bypassed.

## Collector flow

`Registry → Discovery → Metadata Normalize → Prefilter → Article Fetch → Deterministic Extraction → Manual Ingestion domain service → URL/content deduplication → optional existing Analysis pipeline`.

RSS/Atom summaries are discovery metadata only. HTML indexes supply official article links. Only a successful official article extraction may create a Document or enter Analysis. Navigation, scripts, styles, forms, footers, and similar page chrome are removed without an LLM; original language and evidence text are retained.

## Access boundary

Every request uses HTTPS and a registered domain. Redirect destinations are revalidated. Local/private addresses, credentials in URLs, non-default ports, external links, images, attachments, browser automation, login/CAPTCHA/403 bypass, proxies, and anti-detection behavior are prohibited. Requests use a transparent User-Agent, at least a one-second interval per source, 20-second default timeout, and at most two retries for network/5xx failures. 401/403/404 are not retried.

## Deterministic prefilter

DIRECT sources require explicit DRAM/HBM product or monitoring-variable terms. DEPENDENCY_SENTINEL sources require explicit platform, accelerator, AI-server/factory, HBM, memory-bandwidth, deployment, or delay/acceleration terms. General software, gaming, consumer, automotive, robotics, healthcare, and generic AI items are filtered unless they explicitly connect to the dependency chain. Prefilter controls cost only; the unchanged Analysis Adapter owns final Coverage matching.

## Audit and Ops

Every run writes `00-source-fetch.json` with registry/Coverage snapshots, discovery and prefilter results, normalized fetch snapshots, duplicates, filtered items, failures, Document decisions, analyzed IDs, request/retry counts, budgets, and a 16-rule zero-LLM Source Ops result. Invalid, short, metadata-only, filtered, duplicate, or disallowed content cannot enter Analysis.

## Replay

Frozen-source replay recomputes normalization/prefilter and verifies candidate, content-hash, duplicate, and Document decisions from saved local snapshots. It never accesses the network. Live-source replay is intentionally not implemented; users manually run collection again.

## Acceptance

Acceptance requires the existing Phase 0/1 tests to remain green; offline RSS, HTML, security, extraction, prefilter, deduplication, artifact, Ops and replay tests; no migration drift; real dry-run discovery from at least two enabled sources; at least one official Document; and no more than two explicit real analyses.

## Not implemented

No search engine, News API, arbitrary crawler, browser, 403 bypass, PDF/OCR, translation, scheduler, email, frontend, user/auth system, vector database, knowledge graph, new Coverage, Agent framework, trading feature, or Phase 3 capability is included.
