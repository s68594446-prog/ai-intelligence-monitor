# Signal Standard

S0 is noise, duplication, no new fact, or no mapping to a monitored variable. S1 is relevant context that does not alter the current view. S2 is a material marginal change that adjusts the probability of a key variable without changing the core thesis. S3 may change the core thesis or materially change position risk.

Every signal maps to a coverage variable and the exact thesis version. S2/S3 candidates receive a Red Team assessment covering: the assumption most at risk, alternative explanations, confirmation bias, what the market may know, what the item cannot prove, the next validating observation, and worst position risk.

Red Team actions are `CHALLENGE`, `DOWNGRADE`, and `RESURRECT`. `RESURRECT` returns a discarded candidate to adjudication when it may be a major omission. Final adjudication is one of `DROP`, `ARCHIVE`, `INCLUDE_WEEKLY`, `SEND_ALERT`, or `REQUEST_HUMAN_REVIEW`. S3 requires Red Team and adjudication; conflicting evidence cannot be automatically published.

Phase 1 also permits `NO_CHANGE`, meaning the Red Team completed all fixed checks and recommends retaining the proposed signal level. It does not bypass adjudication or Ops Check.

Reverse triggers are observable conditions tied to named metrics. Abstract warnings are not valid triggers. There is no daily Top Five requirement and `NO_MATERIAL_CHANGE` is valid.
