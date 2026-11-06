# November 6 incremental upgrade batches

## Batch 1 — content integrity (already merged in PR 6)

Monetary/source-completeness review and final-copy seals. The old parser missed
`33亿元人民币`; its earlier two HOLD findings were not evidence of catching YMTC.
See INCREMENTAL_REVIEW.md for source fixtures, limitations and rollback.

## Batch 2 — delivery and maintenance

Root causes: the scheduled guard and sender had different duplicate semantics;
content/recipient changes created a new send key; malformed ledgers could pass the
early guard; dates/run paths were independently selected; no independent
Watchdog existed. Local correction/thread prototypes were not deployed and
publication history lacked a link to the actually accepted body and audit.

Changes:

- Shared stdlib ledger: strict records, same-day SENT hold, unresolved-attempt
  hold, local nonblocking lock and flushed durable appends. A successful earlier
  send cannot be undone by a later FAILED row. `force` does not override delivery.
- One workflow day and run path through generation, Ops, preparation and archive.
  Actual MIME remains bound to final review. SMTP acceptance is persisted before
  digest/history archiving, and only SENT can update published history.
- Independent Watchdog at 09:30 Asia/Shanghai and after sending workflows:
  NOT_STARTED, GENERATION_FAILED, REVIEW_BLOCKED, DELIVERY_FAILED,
  DELIVERY_UNCERTAIN, ARCHIVE_FAILED and observation errors are distinct. A green
  generation-only run is DELIVERY_UNCONFIRMED. Only the sanitized report is
  uploaded; there is no custom notification email or automatic retry.
- Confirmed NVIDIA and YMTC correction records contain claim ID, preserved quote,
  source URL, confirmation date, disposition and corrected interpretation. Only
  source-confirmed entries are accepted; ambiguous reviewer findings are not
  automatically added. The registry digest is bound into the final review seal.
- Thread IDs use canonical URL or manually reviewed aliases. Prior publication
  requires matching SENT body/audit hashes, passing Ops and actual reader-item
  membership. Evidence chains retain earlier conclusions, current additions and
  state changes. Changed status without new evidence is held. Three-day chains
  are marked for possible trend review, never converted to a fabricated trend.
  At most five explicit due-date judgments can be configured; no quota, default
  prediction or forecast accuracy objective is introduced. Human confirmation
  with an exact current source quote is required for FULFILLED/NOT_FULFILLED/PARTIAL;
  otherwise PENDING is retained.

No frozen core, department Harness, prompt, provider budget, recipient or original
06:30 digest schedule is changed. API/model cost does not increase. Watchdog adds
a small Actions run and up to three GitHub reads; deployment does not prove future
notification, source availability, SMTP acceptance or inbox arrival.

Operational limits: an offline/stale local checkout is not a distributed lock.
Runner loss after SMTP but before Git persistence still needs reconciliation.
Never auto-resend after ARCHIVE_FAILED or DELIVERY_UNCERTAIN. Actions notifications
depend on account settings. Canonical-URL tracking deliberately misses unrelated
URLs until their event alias is reviewed. Legacy archives without audit/body
hashes are not used as thread memory. Full evidence remains in internal audit;
reader prose is never automatically rewritten from the correction registry.

API/trigger references: [GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[workflow_run events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run).

Each batch is a separate commit/PR and can be reverted independently using
`git revert <batch-code-commit>`, followed by backend tests and CI. Revert later
batches first when a later change depends on an earlier interface. Do not reset
or delete working trees, publication archives or ledgers.

## Batch 3 — next

Mobile email layout, version documentation, dependency lock and stronger offline
replay integrity, preserving the slate palette and all content/delivery gates.
