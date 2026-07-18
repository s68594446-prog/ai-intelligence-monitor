# Source Standard

## Primary-source spine

Core facts should start with T1: company announcements and investor relations, financial statements, call transcripts, SEC/exchange filings, government or regulator originals, official statistics, and formal product, capacity, CapEx, customer, or guidance disclosures.

T2 includes specialist industry bodies, high-quality industry media, authoritative research organizations, reliable financial media, and supply-chain research with a clear source chain. T3 includes X, Reddit, forums, self-media, anonymous supply-chain claims, and secondary reposts.

T3 is discovery-only. A single T3 source cannot trigger S3. General web references are `MONITORED_NOT_TRUSTED` until confirmed by T1 or multiple independent T2 sources. Every source records tier and date.

Every number, date, capacity, yield, price, share, order, or CapEx value must reference an identified source, or be explicitly marked `UNKNOWN`, `N/A`, or `UNVERIFIED`. Guessing, model-memory completion, presenting estimates as facts, and presenting rumors as facts are prohibited. The Coverage Gate returns `INSUFFICIENT_EVIDENCE` instead of manufacturing a high-grade conclusion.

Extracted claims are classified as `OBSERVED_FACT`, `COMPANY_GUIDANCE`, `THIRD_PARTY_ESTIMATE`, `ANALYST_OPINION`, `RUMOR`, or `UNKNOWN`.

For manually supplied text, every extracted claim must also carry an exact evidence quote and the manual document reference. The quote must occur in the preserved raw text; a URL alone is never evidence and is never fetched.

## Phase 2 controlled collection

Phase 2 collection is limited to enabled T1 entries in `config/sources/dram_hbm_sources.yaml`. Each entry fixes its HTTPS discovery URL, exact allowed domains, article URL patterns, discovery method, deterministic relevance/exclusion terms, language, rate interval, timeout, and maximum item count.

- The collection API accepts registered source IDs and limits of 1–10; it never accepts a fetch URL.
- Redirects are followed only while every destination remains inside the same configured host/path allowlist.
- Non-HTTPS URLs, embedded credentials, non-default ports, unsupported content types, oversized responses, changed page contracts, and invalid publication dates fail closed.
- Official RSS full text is preferred. Micron IR uses its registered listing and article-body contracts.
- HTML is normalized deterministically; scripts, styles, SVG, iframes, and navigation are excluded.
- The normalized text is the evidence text preserved in `Document.raw_text`; response hashes and byte counts remain in the fetch audit.
- ETag and Last-Modified are used when available. A 304 response creates no document.
- Content-hash duplicates reuse the existing document and never create a duplicate Event or Signal.
- Fetching and analysis are separate by default. Only an explicit `analyze=true` or `--analyze` may send at most three newly created candidate Documents through the unchanged Phase 1 pipeline.
