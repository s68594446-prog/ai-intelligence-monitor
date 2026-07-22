# Daily China / A-share AI channel

Status: **PARTIAL / offline verified / live fail-closed**

This additive channel is separate from the frozen DRAM/HBM research Harness. It cannot modify S0-S3, Adjudication, SEND_ALERT, or existing Signals, and it never sends email during validation.

## TianAPI boundary

`TianApiAiNewsAdapter` calls only `https://apis.tianapi.com/ai/index`, at most once per run, with `page=1` and at most three results in the production smoke path. `TIANAPI_ENABLED` defaults to false in the template. The API key is a `SecretStr`; query URLs, exceptions, artifacts, and replay never retain it. HTTP 200 responses are accepted only when the internal business `code` is 200. Rate limit, permission, usage limit, unactivated API, and invalid-key outcomes map to stable local codes.

TianAPI states that AI news is limited to internal analysis and machine learning and must not be displayed to end users. Therefore its title, description, and content are `DISCOVERY_ONLY`. Only an original URL may seed discovery. A digest item requires independently fetched official primary evidence.

TianAPI is optional and disabled by default. A disabled or failed TianAPI provider does not represent an empty news day and does not block the independent A-share or daily digest paths.

## Juhe discovery boundary

`JuheHeadlinesAdapter` calls only Juhe news-headlines product ID 235 at `https://v.juhe.cn/toutiao/index`, with `type=keji`, `page=1`, and one request per daily run. `JuheAiBriefAdapter` calls only product ID 850 at `https://apis.juhe.cn/fapigw/aibrief/list`, with `type=tech`, `page=1`, and one request per daily run. The products use independent `SecretStr` keys and cannot share credentials.

Both adapters require HTTP 200 and `error_code=0`. Wrong, unauthorized, expired, rate-limited, over-quota, maintenance, and disabled-product responses map to stable local errors. Request URLs containing keys are never persisted; artifacts store only fixed endpoints, response hashes, safe item IDs, original URLs, request counts, and quota-header availability. A provider error remains an explicit error and is never converted into “no news.”

Juhe titles, headlines, summaries, and generated briefs are `DISCOVERY_ONLY`. They can contribute an original URL and event-cluster provenance but cannot become evidence or raise Source Tier. Final digest content requires a fresh safe fetch from a valid T1/T2 original page. A-share items continue to prefer exchange, CNINFO, or company primary announcements.

Juhe stock product ID 21 is deliberately absent: no key is read, no request is made, and no price, K-line, technical indicator, price attribution, or trading recommendation is produced.

## A-share universe and primary evidence

The deterministic base universe combines the latest official constituent workbooks for CSI Artificial Intelligence Industry Index (`931071`) and SSE STAR Artificial Intelligence Index (`950180`). The normalized snapshot records security code, company name, exchange, index membership, effective date, official URL, retrieval time, and source SHA-256. Duplicate constituents are merged. Refresh only on the official index adjustment cadence. If refresh is safely unavailable, retain the last verified snapshot and mark it `STALE_UNIVERSE`; never substitute a third-party concept-stock list.

Primary sources are SSE, SZSE, BSE, CNINFO, CSRC, and official company investor-relations pages. Only public HTTPS content is allowed. Login, CAPTCHA, anti-bot, signature, and access controls are never bypassed. Media links are supplementary only.

Official PDFs are streamed with DNS validation and IP pinning, redirect revalidation, MIME/header checks, byte and page limits, and SHA-256 provenance. Only existing text layers are extracted. OCR, scripts, attachments, embedded files, and PDF links are not executed. Encrypted, scanned, damaged, oversized, or excessive-page files are `BLOCKED_PDF`.

## Analysis, digest, and replay

`daily-a-share-ai-analysis-v1` is a strict Pydantic contract with exact evidence quotes. Concept-only disclosures cannot pass the importance threshold. Framework agreements are not revenue, planned investment is not delivered capacity, index membership is not a benefit, and price moves are not used to infer causes. The output contains no buy/sell recommendation or target price.

At most four A-share items may appear and they share the China limit of six and whole-digest limit of twelve. When nothing passes, the section says `今日无达到标准的A股AI重大公告`. Frozen-source and frozen-output replay make zero network, search, and DeepSeek calls.

## Latest live audit

The latest A-share run queried four official discovery domains over a deterministic 48-hour window. One official candidate passed discovery, but safe fetch stopped at `NON_PUBLIC_ADDRESS` because the local DNS environment returned a reserved address. No raw text, DeepSeek analysis, or digest event was manufactured. A-share Ops passed the fail-closed result. TianAPI is now disabled. Juhe offline verification passes, but the two Juhe enabled flags and product keys are not currently present in the local Settings environment, so no live Juhe request has been made and the channel remains `PARTIAL`.
