# Offline incremental hardening (2026-10-16)

No new agents, model requests, prompt changes, deployment configuration or mail
schedule changes. Reference design only: williamchen419-eng/us-china-ai-briefing
ARCHITECTURE.md and prompts/04-redteam-editor.md. Its advertised runtime is not
verified here. In particular, do not adopt mandatory issue/prediction quotas,
automatic correction by another model, or delivery on failed review.

## Stage 1 — scoped failures

Adapter budgets are department-local. Terminal 503/timeouts retain their error
category and separately report `halt_scope=department_budget` when exhausted.
Peers continue. Repeated 5xx after bounded recovery report `recovery_exhausted`;
that is evidence about requests, not proof of a provider-wide outage. Auth,
unrecoverable daily quota and the shared deadline still halt globally. A shared
governor also counts every HTTP attempt against the global cap. Allocations,
retry counts, one-time explicit fallback, pacing and all quality gates remain.

Local exhaustion is no longer OR-ed into provider-wide status. Incomplete desks
still block delivery through their existing completion/coverage gates.

Each stage is a separate local commit; revert stages in reverse order. No push,
live model call, SMTP send or production validation is part of these changes.
