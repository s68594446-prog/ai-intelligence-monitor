# Incremental hardening — batch 1 (2026-11-06)

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

The incident fixes are already on main. This batch adds source and final-copy
review independently, under the user's authorization to test and deploy. No live
model call, regeneration or SMTP send is part of this batch's acceptance.

## Stage 2 — final review and sealed MIME bodies

`money_claims.py` and `final_review.py` record monetary claims with snapshot IDs, exact source
spans, source URL, normalized value/currency, period, bound, fact status and unit
(cash/compute access/unspecified). It checks all model prose before admission,
then separately checks parsed final headline/body/analysis/risk text after render.
Ranges, lower bounds, estimates, annual/multiyear values and dated values are
not collapsed into one number. Unsupported claims are HOLD, not confirmed errors.
Explicit annual-allocation non-disclosure claims require review, not invented
replacement text. Internal/debug fields are blocked on all reader surfaces.

`final-review.json` retains the complete claim/evidence audit; Ops carries a
compact seal and finding IDs without duplicating all source excerpts. A passing
review binds date and Markdown/plain/HTML hashes. Email re-renders and verifies
those hashes and the actual MIME inputs before SMTP or ledger mutation. Old
archives without a seal now need offline re-review from preserved sources; a
historical green Ops result alone cannot authorize sending them. The workflows,
schedule, recipients, SMTP settings, prompts and models are unchanged.

`DailyArticleExtractor` prefers article/articleBody, then main, and otherwise
uses cleaned legacy text. Navigation, sidebars and footers cannot inflate a
short teaser into a usable article. Inline markup does not split evidence quotes.
Explicit paid-access or truncation notices block the source before model input,
including notices outside the article container. Newsletter signup alone is not
an access barrier. The shared safe fetcher has an optional extractor hook; its
default, SSRF protections, request limits and other callers remain unchanged.

Final review checks primary and supplemental links, required card fields,
non-numeric prose lost during parsing, all rendered monetary values, and internal
field leaks. The actual MIME text/HTML must match the saved seal, not merely a
second re-render that could reproduce the same corruption. No error quota is used.

Limits: this deterministic parser currently gates explicit monetary values in
USD/CNY/EUR/GBP/HKD/JPY/TWD/KRW and nearby time/status qualifiers. It is NOT a universal
numeric or semantic verifier. Non-monetary metrics retain existing Evidence
gates; ambiguous units, attribution across multiple subjects, dates and scope
still require human review. Equal numbers on a page do not prove equal meaning;
`SUPPORTED_VALUE` deliberately does not claim that. No FX conversion or derived
annual allocation is invented. The hash is a consistency seal, not a signature
against an attacker able to replace both body and audit.
Extraction also cannot certify completeness of arbitrary unmarked HTML: unusual
layouts, hidden CSS paywalls, attribution across sentences, derived sums and
non-monetary metrics remain review limitations. A HOLD is not a confirmed error.
The stricter admission checks can reduce news count or stop delivery; they do not
authorize dropping failed departments or lowering importance/Evidence standards.
Default daily model/API cost does not increase; blocked incomplete sources avoid
analysis calls. Only local deterministic parsing and audit storage are added.

Replay without model/network/SMTP:

```
python -m app.scripts.review_daily_snapshot --run-dir /path/to/saved-run --day 2026-10-15
```

Fixed cases cover the October NVIDIA USD250m/USD500m conflict, annual-disclosure
claims and internal raw_text leaks. November 6 replay of run 42164750075 checks
13 reader monetary claims and produces seven HOLD findings: two YMTC unit errors,
three explicit incomplete-source notices and two unsupported Treasury aggregate
claims. Only the YMTC unit error is confirmed here; the latter two findings do
not establish that the calculation is false. The old reviewer's two HOLD findings
did NOT catch YMTC: it failed to parse `33亿元人民币`. The current regression
explicitly checks CNY33 billion = 330亿元 and keeps access completeness separate
from correct arithmetic. It never treats the teaser as confirmation of a current
IPO or actual payment.

Rollback: revert the batch's code commit as a unit (or the merge with mainline 1),
then run the backend suite and deploy the revert through CI. Do not reset or
delete working trees. Old archives without a matching review seal remain blocked
under this version and require offline source-based review before any separately
authorized resend. Changing renderer output later also requires a new review.

Acceptance results and commit/CI references are reported with the deployment;
offline fixture success is not proof of the next live source/model/email run.
