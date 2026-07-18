# Product Contract

AI Intelligence Monitor is a personal research aid that continuously evaluates only user-selected industry coverage and the dependencies required to understand it. It is not a general AI-news search engine. The user selects a coverage topic; versioned coverage defines entities, products, monitored variables, exclusions, thesis assumptions, and observable reverse triggers.

Dependency sentinels extend direct monitoring only where a variable can affect the selected thesis. For DRAM/HBM this includes GPU demand, cloud CapEx, AI-server shipments, CoWoS capacity, and export policy. Items that cannot map to a monitored variable are out of scope.

The system distinguishes evidence from judgment, preserves a source chain, and may produce `NO_MATERIAL_CHANGE`. Zero alerts is a normal and desirable output when no evidence-backed marginal change exists.

The system does not predict prices, recommend trades, execute transactions, make autonomous investment decisions, or replace human judgment. Phase 0 additionally has no network collection, LLM, Gmail, email delivery, frontend, accounts, multi-tenancy, or deployment automation.

Phase 1 permits only user-supplied DRAM/HBM text and an LLM behind the existing structured adapter boundary. A supplied URL is stored but never fetched. Harness, Coverage Gate, Red Team, adjudication, local rendering, and zero-LLM Ops Check remain authoritative.

Phase 2 permits network reads only from active entries in the versioned official-source registry. The user manually chooses a registered source key; arbitrary URLs are not accepted. Collection normalizes and deduplicates official text, records a fetch audit, and stops at `COLLECTED`. It does not call an LLM until the user separately requests analysis through the existing Phase 1 path. Coverage, Thesis, Harness, state machine, prompts, schemas, Evidence Gate, Ops Check, Replay, and provider behavior remain unchanged.
