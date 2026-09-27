# Street Story backend · DevCoveer runtime

This is the deployment boundary for the backend source in `backend/`. It intentionally contains no Fly.io plan and no provider credentials.

## Runtime shape

- Python 3.12.
- One FastAPI/Uvicorn process with one in-process durable worker; SQLite is the local source of truth.
- Persistent `DATA_DIR`, recommended `/var/lib/street-story`.
- SQLite WAL plus `synchronous=FULL`.
- Service supervisor with restart policy; see `backend/deploy/street-story.service.example`.
- Reverse proxy/TLS in front of local Uvicorn; Android receives HTTPS only.
- VibePublish is a separate persistent DevCoveer service. Street Story uses only its bearer-protected HTTP API.

## Required runtime configuration

Create a Python 3.12 environment, install `backend/requirements.txt`, place the repository at `/opt/street-story` (or equivalent exact checkout), and configure the systemd example. Populate runtime secrets locally; never commit them.

Required values:
- `STREET_STORY_DEVICE_TOKEN`
- configured Google key pool credentials for ordinary transcription/research
- `AI_RESOURCE_CONTROL_URL`
- `AI_RESOURCE_CONTROL_SERVICE_KEY`
- `GOOGLE_API_KEY3` as Street Story's dedicated emergency Live fallback source; trusted backend maps it to `AI_RESOURCE_CONTROL_FALLBACK_KEY`
- optional `AI_RESOURCE_LEDGER_ID` after the shared Live migration has been verified
- `VIBEPUBLISH_BASE_URL`
- `VIBEPUBLISH_BEARER_TOKEN`
- `VIBEPUBLISH_HTTP_HOST=mcp-vibepublish.kenigevents.ru` when the local loopback VibePublish service runs behind its OAuth public-host boundary
- `DATA_DIR=/var/lib/street-story`

Ordinary transcription/research keeps the existing request limiter semantics. Managed Live sessions use the private
`ai-resource-control v0.1.5` lease SDK plus public `live-interaction v0.1.4`. The installer builds the private
controller wheel from the exact accepted private commit and never vendors that private source into this public repository.

Do not substitute generic product `SUPABASE_URL` / `SUPABASE_KEY` for the dedicated Google AI limiter authority.
Application runtime configuration never falls back to generic Supabase aliases. The DevCoveer installer pins the canonical
limiter origin to `https://epyznmylqmchteykjsqj.supabase.co`. If dedicated limiter aliases are not present yet, it may
consider an existing server-side **service-role key alias only** as a candidate (including the established KenigEvents
`PERSONALIZATION_SUPABASE_SECRET_KEY` alias), never a generic URL: the candidate is promoted only after a read-only call
to `google_ai_limiter_capabilities()` on that canonical origin authenticates it and returns the
exact `google_ai_project_model_atomic_v1` / `google_cloud_project` contract. A key for any other Supabase project therefore
cannot silently become the quota authority.

If the shared Live RPC/migrations or a verified canonical credential are absent, Live starts fail closed; Street Story must
not fall back to a direct API key or to the legacy async voice path.

Before any service restart, the DevCoveer installer performs a read-only shared-resource preflight through the pinned
private SDK. It verifies the legacy limiter contract, the `ai_resource_leases_v1` capability surface, a nonempty
ledger id, and at least one eligible registered key. The preflight does not acquire a Live lease and does not call
Gemini; failure leaves the currently running Street Story release untouched.

Ordinary grounded internet research uses Google Search grounding on `gemini-3.1-flash-lite` first and falls back to `gemini-3.5-flash-lite`; both use the canonical shared-limiter policy 15 RPM / 250k TPM / 500 RPD per registered Google project. Transcription remains independently routed through `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`. Keep an identifying OSM User-Agent.

## Exact source SHA gate

`GET /healthz` returns `ok=true` and `source_sha`. For PR Live E2E, `source_sha` must equal the exact current PR HEAD. A missing/mismatched SHA is a deployment failure and the workflow stops before product effects. Do not disable this gate and do not test a newer source harness against an older deployed backend.

When redeploying a source checkpoint, update **both Street Story runtime endpoints** to the same exact checkout, set `STREET_STORY_DEPLOY_SHA` when the deployment mechanism uses it (or otherwise ensure checkout-derived SHA is authoritative), restart the service, then read back `/healthz.source_sha` from both endpoints before dispatching Live E2E.

## Restart/recovery semantics

Admission handlers commit durable state only. Research, visual, publication and cancellation are leased SQLite jobs. Expired `running` jobs are reclaimed on restart. Per-chunk transcripts are persisted, so confirmed chunks are not retranscribed merely because the process restarted.

Raw ASR and cleaned `display_text` are persisted separately. Restart/readback does not recompute cleaned owner text once stored.

VibePublish identity is committed/recoverable across every external effect:

- source image ingress uses a stable idempotency key and immutable source SHA;
- visual command and candidate selection use stable keys and operation readback;
- publication intent stores the exact VibePublish request key before external admission;
- cancellation stores a stable command key and reconciles the existing publication revision.

Lost HTTP responses therefore repeat the same semantic key rather than create a new asset, visual job, publication or cancellation.

## VibePublish requirement

Runtime must expose the VibePublish lineage represented by public commit `dec1c69920f09ebdc0551132e6bdffba73d03e8e` or a compatible newer contract, including:

- authenticated `POST /v1/assets` source-image ingress;
- authenticated asset readback;
- visual command + official selection + operation reconciliation;
- publication scheduling/status;
- publication cancel command/status.

The previous Street Story `visual_blocked / vibepublish_media_ingress_not_enabled` boundary is obsolete. Do not reintroduce it by pinning an older VibePublish runtime. Also do not bypass the contract via VibePublish SQLite, a second Imagegen, or direct Telegram/VK publication.

## VibePublish resident service

VibePublish is supervised independently on DevCoveer and reachable through `VIBEPUBLISH_BASE_URL`. Its own deployment/runbook remains authoritative for provider credentials, native workers and Imagegen. Street Story stores none of those provider secrets.

## Gemini reliability and shared limits

See [Gemini P0 reliability](gemini-reliability.md). Ordinary request work keeps the existing
`reserve → mark_sent → provider → finalize` authority.

Live capacity is a separate lease shape in the **same dedicated limiter**, not a second quota database or gateway.
Street Story calls `ai_resource_control.run_guarded(consumer="street-story", ...)` with only the central authority URL/service credential
and an opaque per-session binding. The authority atomically chooses a quota scope and returns the selected provider key only as a lease-bound encrypted envelope. A classified credential/quota/capacity
fault may select another scope only before provider `ready`; after `ready` the conversation remains pinned to one
key/scope through resumption. Expired/fenced resource state is terminal and cannot trigger a hidden direct-key retry.

Provider Live RPM/RPD being reported as Unlimited does not imply unlimited concurrency. The common controller keeps
finite local admission and records unexposed provider concurrency honestly. Production authority migrations 001–008 and Vault bootstrap are complete; each consumer still remains fail-closed until its central capability preflight succeeds.

## Authority-outage fallback

Street Story owns exactly one emergency Live alias: `GOOGLE_API_KEY3`. Normal Live sessions use the central Vault-backed authority and do not receive the ordinary local provider-key pool. The trusted backend maps the assigned alias value into the single generic `AI_RESOURCE_CONTROL_FALLBACK_KEY`; the original alias and the rest of the Google pool are not forwarded to `ai-resource-control`.

The shared SDK may use the mapped `AI_RESOURCE_CONTROL_FALLBACK_KEY` only when the initial read-only authority capability probe returns `RESOURCE_CONTROL_UNAVAILABLE`, before any mutating acquire. It must not activate for admission/quota/429/capacity/credential decisions, after a successful authority probe, after a lost acquire response, or after provider ready. Emergency mode still uses the shared `resource_guard`, permits one local Street Story fallback session per host and expires after two hours.

The app must never borrow Wonderful Lections' `GOOGLE_API_KEY`, KenigEvents' `GOOGLE_API_KEY2`, Projects Hub's `GOOGLE_API_KEY4`, or shared reserve keys 5–6 for this fallback. Android never receives any provider key.

## DevCoveer fallback smoke runner

If GitHub Actions accepts `live-e2e.yml` but never allocates a job, DevCoveer may run the same repository-owned smoke through `backend/tools/devcoveer_live_smoke.py --expected-sha <deployed-sha>`. The runner does not install host packages: it uses a disposable official Python/Debian Docker container, installs `espeak`, `ffmpeg`, ExifTool and `httpx` only inside that container, mounts repository source read-only and writes only the sanitized Live E2E diagnostic.

The runner refuses to mint a device token: the existing 0600 token file must already exist before it calls the deploy module's `device_token()` helper. The helper's existing idempotent GitHub secret synchronization remains the only allowed credential-side effect. The token is passed to Docker by environment-name inheritance rather than argv, child stdout/stderr is not emitted, and the named container is force-removed in `finally` if the smoke times out. This is a production acceptance rail and therefore creates the same smoke-mode Street Story/research/provider side effects as `live_e2e.py`; it does not schedule Telegram publication.

## Live E2E acceptance

The current acceptance path is **Live-only**. The old voice-session/M4A HTTP protocol is not exercised as a fallback.

The Android golden run uses prepared PCM only after the capture/VAD boundary (so CI does not pretend to have a physical
microphone) and then follows the production realtime path:

1. exact deployed source SHA and authenticated topic;
2. one Gemini Live session under the shared resource lease;
3. multiple Russian realtime turns through the bounded Android PCM queue;
4. explicit research with real source readback;
5. fact selection and iterative text editing;
6. literal/verbatim dictation with protected-span preservation;
7. a subsequent edit plus Undo;
8. visual-only change proving the text revision is unchanged;
9. verified VibePublish visual asset and readback;
10. exact publication confirmation card;
11. provider-native Telegram schedule/readback/cancel on an explicitly safe test destination.

The evidence must say `physical_mic=false`, `prepared_pcm_after_capture_boundary=true` and
`legacy_voice_endpoint_used=false`. A legacy cancellation call is allowed only as best-effort emergency cleanup after
a failed test; it can never make acceptance green.

The long-lived async voice endpoints remain available for compatibility and possible future development, but a Live
failure is surfaced as a Live/resource error. There is no automatic route switch to those endpoints.
