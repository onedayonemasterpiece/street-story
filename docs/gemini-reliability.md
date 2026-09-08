# Gemini P0 reliability

## Scope and prior infrastructure

Backend-only; one process, SQLite WAL/FULL, unchanged Android states. No visual,
social, prompt or Android implementation changes are part of this layer.

Inspected DevCoveer's existing `vibepublish/google_ai/{client,secrets}.py`, matching
`events-bot-new/google_ai/client.py`, the festival parser token bucket, and the
shared gateway requirement/migrations. The full GoogleAIClient is coupled to
Supabase reserve/mark-sent/finalize, text/model fallback policies and returns text
rather than the grounded multimodal response needed here. Its local fallback is
not durable per-key/per-operation health. Importing that entire gateway would
change the runtime/dependency boundary, so Street Story uses a small local pool
and reuses its existing secret-ref and authoritative quota RPC contracts componentally.

Safe read-only inspection of the already configured DevCoveer ledger returned
HTTP200 for `google_ai_model_limits` (model gemini-3.1-flash-lite:13RPM,
240000TPM,450RPD) and metadata for five active refs `GOOGLE_API_KEY` through
`GOOGLE_API_KEY5`. The existing secret environment also has `GOOGLE_API_KEY6`;
an unregistered key is NOT eligible for Street Story provider calls. These are
observed owner-ledger settings, NOT fresh Google quota guarantees.

## Configuration and secret handling

- Preferred: `GEMINI_API_KEY_REFS` JSON list of explicitly selected env names.
  Both indexed conventions (with/without underscore) work because names are refs,
  not guessed contiguous slots. Missing refs fail safe without their values.
- Alternative: private `GEMINI_API_KEYS` JSON string list, at most32 slots.
- No explicit pool: legacy `GEMINI_API_KEY` becomes a one-element pool.
- Explicit pool takes precedence; order stable, duplicate values removed.
- Secrets are SecretStr objects in Settings: repr/asdict plus JSON `default=str`
  are redacted. Reveal occurs only at transport/auth boundaries. Do not manually
  inspect internal SecretStr attributes or dump environment variables.
- **Mandatory for provider calls:** `GEMINI_QUOTA_SUPABASE_URL` and private
  `GEMINI_QUOTA_SUPABASE_KEY`, with existing reserve/mark_sent/finalize RPC access.
  Missing configuration leaves durable jobs retryable; it never enables local bypass.
- Registered env names must exist locally and match configured secrets. Legacy
  single-key / JSON pools work when their value also resolves to a registered env
  ref. Unregistered or inactive credentials cannot reach Gemini.

## Mandatory shared accounting

Every native Gemini attempt, for **both** workloads, follows:
1. Drain durable pending accounting; resolve an active shared-registry key ID.
2. Persist a private SQLite intent, then `google_ai_reserve` with **only that
   candidate ID**, a unique request UUID, attempt 1 and the configured model.
3. Require successful `google_ai_mark_sent` and read back its receipt.
4. Only then issue the native SDK request (one SDK attempt).
5. Commit an outbox record with actual usage/error category. Worker/next call uses
   `google_ai_finalize` and verifies the finalized receipt before removing it.

No local fallback, legacy RPC fallback, model fallback or missing-RPC exception.
Controller outage, malformed receipt or ambiguous reservation => **no new provider
call**, durable retry after 30 seconds. A known per-key reserve denial tries another
healthy slot without sending a provider request on the denied key. RPC RPD denial
uses the ledger's UTC bucket reset; Google itself may impose different limits.

Each failover attempt gets its own UUID because the shared finalize contract is
request-level idempotent. Unknown reserve/mark-sent outcomes are journaled; after
an in-flight grace deadline they are read back and reconciled, never re-reserved.
Unknown provider usage remains NULL, conservatively retaining reserved TPM. Lost
finalize responses are retried using the same UUID; no successful provider result
is discarded just to retry accounting. Outbox recovery also runs while idle.

Token reservations follow the existing gateway's estimated-input + output-budget
+ margin convention, not a fictional exact token/remaining-quota API. Here text
uses UTF-8 bytes, images reserve 8192, audio at least 8192 or bytes/4, plus 8192
bounded output and 1000 overhead. Actual usage reconciles the shared counters.
These estimates are not strict token upper bounds. The existing shared controller
remains responsible for cross-application limits; Street Story does not rewrite its
SQL or claim other consumers' fail-open behavior has been repaired.

## Scheduler, classification and bounded retries

`GeminiKeyPool` persists hashed-key identity, model/workload health, cooldown,
failures, success, retry metadata and local minute accounting. Credential-level
invalid authentication is persisted separately. Key order changes preserve health;
replacing a credential gives a new identity. No key value is stored in these tables.

Within this process a reservation allows one in-flight call per key, across
workloads. Selection excludes disabled, cooling, reserved and known-exhausted
keys; then prefers fewer failures, lower known utilization/local usage and oldest
selection. A durable reservation deadline (call budget+5seconds) protects an
unknown in-flight outcome across process restart. Live reservations are released
on success, failure, timeout and cancellation. Do not run multiple workers or
processes against this store as a distributed limiter.

Two operation classes: `transcription`, `grounded_research`. Provider429 affects
only the observed class. Auth-invalid disables the credential across operations.
A known shared model quota may legitimately cool both classes. Success resets
that operation's failure/cooldown state, never another workload's evidence.

Level A (one semantic job attempt): at most one try per distinct eligible key,
maximum32, total default60seconds per Gemini operation, default20seconds per key.
Both budgets are configurable. Native async google-genai calls are cancellable;
SDK retries are explicitly set to1 so hidden same-key sleeps cannot bypass the
pool's budget. The next healthy key is tried immediately without a cooldown sleep.

- 429/RESOURCE_EXHAUSTED: cool current operation; fail over immediately.
- Timeout/network/5xx/overload/transient SDK failures: short cooldown; fail over.
- Malformed response: safely retry via another eligible key in the same budget.
- Invalid request/schema/unsupported model: permanent operation error, no key storm.
- Auth-invalid: disable key, try other keys. If all disabled/missing, preserve the
  job as a retryable runtime problem so new configuration can recover it.

429 fallback cooldown:60seconds, exponentially grows to3600seconds per key/class.
Other transient fallback:5seconds growing to60seconds. Honor the larger of that
backoff and valid Retry-After (seconds or HTTP-date) / Google RetryInfo retryDelay.
Malformed/infinite/negative delays are ignored; metadata delays capped at7days.
No fallback to a different model or ungrounded research.

Level B: only when bounded failover cannot succeed, schedule SQLite `available_at`
from the earliest meaningful cooldown/reservation/local-rate/advisory deadline,
with a minimum1second. No configured/live keys:300seconds. Non-pool retry errors
retain existing exponential job backoff. There is no attempt-count terminal cutoff
for transient exhaustion. Job lease heartbeat renews the existing90second lease
while long multichunk/failover work runs. Restart reclaims expired jobs normally.

## Checkpoints and phone boundary

Existing durable photo/chunks, per-chunk and aggregate transcripts remain intact.
Additive `research_checkpoints` retains successful OSM, Wikipedia and grounded
Gemini stages per semantic job; completed stages are not fetched again on provider
failover, durable retry or restart, even after generic provider cache expiry.
Refinement has a new semantic job and therefore a fresh checkpoint namespace.
Checkpoints have the same private retention boundary as the story itself.

During automatic recovery: `state=researching`, `error=null`. Additive `processing`
metadata contains `automatic_retry=true` and a neutral Russian message. After
30minutes (configurable), `processing.status=processing_delayed` and the message
is “Обработка займёт немного больше времени”; **story.state stays researching**.
No Android UI/state-model change, raw429, key identity or new-photo request.
Permanent operational errors use a neutral message; diagnostic error categories
remain internal. Aggregated per-operation pool counts appear only in authenticated
`/v1/capabilities`; public `/healthz` stays liveness-only.

Logs contain only operation, hashed key prefix, slot, category/status, cooldown,
failover count, latency and all_keys_unavailable. Never prompt, photo/audio bytes,
Authorization or raw provider exception strings. Failed stage/job metadata is
bounded and redacted. Raw SDK error details are used only transiently to classify.

## Tests and safe live verification

`backend/tests/test_gemini_reliability.py` covers requested cases1–15, including
same-job failover, Retry-After, all-exhausted durable recovery, operation separation,
concurrent jobs, checkpoint reuse/restart, no raw errors/secrets, cancellation,
actual timeout budget, SDK retry disabling/close, and mandatory shared quota enforcement (`test_shared_quota.py`).
Existing tests remain unchanged. Run compileall, Ruff and the complete pytest suite.

After backend CI succeeds, deploy the exact PR head with existing key refs. Public
runtime admission and internal structured logs demonstrate naturally observed
failover. Optional operator-only `tools/gemini_failover_probe.py` runs the exact
service/pool with real providers in an explicitly isolated DATA_DIR, injecting
only the first retryable error. It never synthesizes a successful provider result,
publishes socially, modifies the resident data store, or exposes test controls via
HTTP. Its report distinguishes actual downstream success from all-keys exhaustion.
No standalone simulated result may be presented as full public live E2E.

Do not bypass GitHub default-branch visibility to dispatch live-e2e.yml. Green
backend CI, public runtime verification and a green full live E2E are separate claims.

## References

- https://ai.google.dev/gemini-api/docs/rate-limits — quotas are per project,
  not per key; no guessed cheap Google remaining-quota endpoint is used.
- https://ai.google.dev/gemini-api/docs/troubleshooting — transient provider errors.
- https://googleapis.github.io/python-genai/ — native async client / HTTP options.

Official pages were retrieved with HTTPS and compared with installed SDK sources;
the web-tool and independent subagent sessions were unavailable due expired/revoked
tool credentials during discovery. Shell/GitHub access remained available.
