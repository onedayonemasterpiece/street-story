# Shared Live resource candidate

`create_live_host` now wires `managed_provider(settings)` from `backend/street_story/live_resources.py`. The old host's `key_resolver` receives a non-secret compatibility marker only; managed_provider ignores load_key and never passes the marker to a provider. Shared SDK selects a verified candidate key using the existing quota authority. This removes first-key/LIVE_API_KEY selection from the actual Live factory without modifying document/publication behavior.

Only explicit GOOGLE_AI_LIMITER_* configuration and named key aliases enter the SDK. Settings.gemini_key_refs can provide aliases, not a synthetic mapping of raw keys. No product Supabase or ordinary quota setting is implicitly reused. Binding is currently service-wide (3-session ceiling); project/actor authorization remains in the existing host.

**Blocked rollout, not provider acceptance:** private controller package must be installed through deployment, the shared live-interaction transport must gain its resource_guard contract and abandoned-client cleanup, and actual per-scope Live quotas must be verified. The common transport mutation was blocked and not retried; SDK refuses an incompatible old transport before reserving or calling Google. Requirements are deliberately not pinned to an invented new commit/version.

No Android/backend runtime deployment or restart was performed. This PR is based on the existing Live feature checkpoint, not an assertion that it is in main. Ordinary quota.py/outbox, backend authentication, STREET_STORY_DEVICE_TOKEN and publication confirmation remain unchanged. Existing ordinary config product-Supabase fallback and UTC RPD fallback remain follow-up defects requiring runtime-config-aware cutover; this Live candidate does not claim to have fixed them.

Tests: `PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_live_resources.py -v`. Run the existing Live/editor/publication and shared-quota suites as regression before any merge; live microphone/concurrency/reconnect acceptance remains mandatory.
