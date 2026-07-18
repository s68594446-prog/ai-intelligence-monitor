# Phase 3A — Controlled Email Delivery

## Status

Phase 3A is `PARTIAL` until a separately approved test-mailbox delivery is completed. This implementation and its tests do not send real email.

## Scope

Phase 3A adds one bounded path:

`existing Analysis Run → SEND_ALERT → Ops Check PASS → existing 05-brief.html → DeliveryFeedback`

It does not add collection schedules, Cron, Celery, Redis, provider-management UI, Gmail/Outlook APIs, attachments, tracking, new Coverage, or changes to research conclusions.

## Configuration

Copy `.env.example` to the ignored `backend/.env`. Real SMTP credentials must never be committed or pasted into chat.

- `EMAIL_DELIVERY_ENABLED=false` controls all email delivery.
- `EMAIL_AUTO_SEND=false` controls the post-Ops coordinator.
- `SMTP_SECURITY` accepts only `STARTTLS` or `SSL`; plaintext SMTP is unsupported.
- `SMTP_TO_ADDRESSES` is a comma-separated list and fails closed when empty or invalid.
- `SMTP_MESSAGE_ID_DOMAIN` is optional; the From-address domain is the fallback.

The SMTP password is held as a secret setting and is never written to `DeliveryFeedback`, artifacts, results, logs, or exceptions.

## Deterministic Gate

`app/delivery/service.py` blocks delivery unless every condition is satisfied:

- Run exists, is an original `manual` Run, and is `PASSED`.
- `replay_of` is empty and `ops_check.passed` is true.
- The Run has `LIVE_MODEL` manual-ingestion provenance, validated live-adapter audit metadata without test/fake model markers, and a database Document matching the immutable manual-input snapshot.
- The cached alert count exactly matches the actual `SEND_ALERT` Signals and at least one alert was selected.
- All Signals are final S2/S3 `SEND_ALERT` Signals in `OPS_CHECKED`.
- Every alert Signal has schema-valid Analysis and Red Team payloads consistent with the database Signal and audited state transitions.
- No conflicted, unverified, insufficient-evidence, rejected, failed, or out-of-scope Event exists.
- The audited `05-brief.html` exists inside the exact Run directory.
- The Brief is a non-symlink regular UTF-8 file no larger than 1 MiB.
- The Brief's Ops markers are both `PASSED`.
- Email is explicitly enabled and SMTP configuration is complete.
- No prior email attempt exists for the Run.

Replay, fixture, S0/S1, non-alert adjudications, unsafe paths, missing Briefs, configuration errors, and uncertain prior attempts fail closed.

## Idempotency and audit

The deterministic idempotency key hashes the Run ID, `EMAIL` channel, exact Brief hash, and SHA-256 recipient fingerprint. The deterministic Message-ID derives from that key.

Before the network call, the service commits an `EMAIL_PENDING` `DeliveryFeedback`. Database uniqueness protects both the idempotency key and `(run_id, delivery_channel)`, enforcing at most one email attempt per Run. Results are recorded as `EMAIL_SENT`, `EMAIL_FAILED`, or `EMAIL_UNKNOWN`. Pending, failed, or unknown attempts are never automatically retried and require manual review.

SMTP cannot guarantee absolute end-to-end exactly-once delivery: a connection may fail after the remote server accepts the message but before the client receives confirmation. Such cases are recorded as `EMAIL_UNKNOWN` and remain fail-closed. Phase 3A deliberately has no automatic retry queue.

## Adapter and content security

The SMTP Adapter sends the existing HTML Brief unchanged as the HTML alternative and supplies a simple plain-text fallback. It does not call an LLM, rewrite the Brief, load external images, follow links, add scripts, add tracking pixels, or attach files. Addresses and headers reject control characters and header injection. Multiple recipients are supplied only as SMTP envelope recipients; the visible `To` header uses `undisclosed-recipients:;`. Partial recipient acceptance is recorded as `EMAIL_UNKNOWN` and is never retried automatically. Provider errors are converted to bounded codes without credentials, recipients, or message bodies.

## Automatic boundary

The Harness and Ops Checker remain unchanged. After an original manual Analysis returns, `app/manual/service.py` calls a small post-Ops coordinator only when `EMAIL_AUTO_SEND=true`. Replay and fixture paths do not use this coordinator. Email failure never changes the Run status, Signal, adjudication, Ops result, or research conclusion.

## CLI

Dry-run Gate inspection is the default and performs no network call:

```bash
cd backend
python -m app.scripts.deliver_email --run-id <RUN_ID>
```

Real delivery requires both valid local configuration and the explicit `--send` flag:

```bash
python -m app.scripts.deliver_email --run-id <RUN_ID> --send
```

The second command is reserved for the next separately approved test-mailbox acceptance. CLI output contains counts, fingerprints/IDs, and safe reason codes—not SMTP passwords or full recipient addresses.

CLI exit codes are `0` for READY/SENT/ALREADY_SENT, `2` for deterministic Gate blocking, and `1` for failed or unknown delivery outcomes.
