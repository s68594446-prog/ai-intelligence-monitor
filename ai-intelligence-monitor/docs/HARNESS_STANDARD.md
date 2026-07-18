# Harness Standard

The controlled pipeline is `COLLECTED → NORMALIZED → DEDUPED → TRIAGED → ANALYZED → RED_TEAMED → ADJUDICATED → RENDERED → OPS_CHECKED`. Terminal or held states are `OUT_OF_SCOPE`, `DUPLICATE`, `UNVERIFIED`, `CONFLICTED`, `INSUFFICIENT_EVIDENCE`, `REJECTED`, and `FAILED`.

Only Harness code changes persistent state. Collector, Analysis, and Red Team adapters return Pydantic-validated structured data and have no database session. Invalid JSON, missing fields, and illegal enums are rejected and audited. Every transition is checked against the transition graph and written to `state_transition_audits`; direct jumps such as `COLLECTED → RENDERED` fail.

Each run stores coverage, thesis, prompt, fixture, and adapter versions plus five ordered artifacts. Ops Check uses no model. It verifies artifacts, evidence for S2/S3, numeric sourcing, S3 Red Team, T3 restrictions, event uniqueness, transition legality, version recording, publication holds, and brief/adjudication consistency. Failure sets `FAILED_OPS_CHECK` and blocks delivery.

Replay reads the original fixture input and frozen coverage/thesis/adapter configuration, creates a new run, never overwrites the original, and saves a structured diff of event count, signal levels, Red Team actions, adjudications, and brief summary.

Phase 1 manual runs preserve the complete input snapshot, model names, separate analysis/Red Team prompt versions, adapter versions, and raw schema-validated outputs. Frozen-output replay reuses those outputs; live-model replay calls the configured current model and records explicit differences. Deterministic fixture replay remains unchanged.

Phase 2 source-collection runs add `00-source-fetch.json` before any optional Phase 1 artifacts. Zero-LLM Source Ops validates registry membership, HTTPS/domain allowlists, extraction status, deduplication, budgets, provenance, 403 behavior, and analysis eligibility. Frozen-source replay reuses only saved discovery, normalized text, registry, Coverage, and prefilter snapshots; it never accesses the network.
