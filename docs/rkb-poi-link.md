# Regional Knowledge Base → Street Story POI bridge

One model owns the decision: a physical/historically memorialized site is worth a POI, independent of whether its original building still stands. A location can be larger than a POI, e.g. island, district or street; never create a POI per source chunk.

RKB stages source-backed locations/persons/events/stories first. The model calls RKB `poi_registry(search)` for physical candidates. It explicitly `select`s the existing Street Story POI or `create`s a new `candidate` site (standing / ruins / memorial / lost_site / historic_site), attaching that canonical owner ID to the SAME RKB entity.

The RKB model does not insert rows into Street Story DB. Its authenticated MCP calls Street Story `POST /v1/internal/rkb-poi` over fixed loopback with an independent scoped service credential. The owner uses the existing canonical `pois` registry and a small `rkb_poi_links` table for the inverse source-identity pointer; no source text is replicated. After owner receipt, RKB updates its external_ref. A lost response is recovered by stable idempotency, not a second POI.

Only `search` suggests matches. `select` checks the owner ID exists, `create` rejects unreviewed same-name/external-ID candidates; the model, not the backend, evaluates site equivalence. Creation does not verify historical facts or invent coordinates.

Street Story can read historical material from a POI using its device-authenticated `GET /v1/pois/{poi_id}/knowledge`. Backend then requests RKB bounded source context with a dedicated loopback grant, never forwarding the Android bearer or an end-user RKB OAuth token. RKB checks its actor's source ACL and returns entity ID, evidence pointers, directly linked stories and one-hop persons/events. Details are fetched using RKB evidence tools when necessary.

The service bridge is disabled unless privately configured. Existing Street Story `STREET_STORY_DEVICE_TOKEN` remains unchanged. Current source-ingestion contract: [RKB location registry](https://github.com/onedayonemasterpiece/regional-knowledge-base/blob/main/docs/location-registry.md).

## Book facts into the POI registry (2026-10-18)

Owner decision: when RKB imports a book it adds the book's POI facts to Street Story,
creating a POI when the place is new. Until this date no event had ever arrived
(`poi_external_events` was empty): Street Story could parse `poi.fact_evidence.v1`
but exposed no intake.

**Intake.** `POST /v1/internal/rkb-poi-evidence` on Street Story's loopback listener
(`http://127.0.0.1:8188`), one `poi.fact_evidence.v1` event per request (≤ 64 KiB),
`Authorization: Bearer <STREET_STORY_RKB_POI_SERVICE_TOKEN>` — the dedicated RKB service
key, never a device token or a user's OAuth bearer. Idempotent by `event_id` and
`idempotency_key`: a replay of the same payload returns the stored receipt with
`replayed: true`; a different payload under the same keys is `409`. Invalid events are
`422` with the normalizer code. RKB keeps the event in its outbox until it holds a 2xx
receipt; a timeout is retried with the same event.

**Identity.** Known external ids (wikidata, osm, wikipedia) attach to the existing POI;
unknown external ids create a `candidate` POI with those aliases; a name-only locator that
matches zero or several POIs stays `unresolved_identity` (no POI is created by name).

**Readback.** `GET /v1/internal/rkb-poi-evidence/{event_id}` returns the three
confirmations: `delivered`, `poi_resolved` (+ `poi_id`, `state`) and the fact accounted
(`claim_id`, `claim_status`: candidate / accepted / contested / rejected), plus
`superseded` when a newer revision of the same book exists.

**Use in stories.** A book claim enters a story's fact list only when `accepted`
(review state), and only from the newest revision of its book: a corrected import
(higher `source.revision` for the same `document_ref`) supersedes the earlier revision's
evidence. Candidates remain research hints for the house's researcher.

**Provisioning (2026-10-19).** The RKB↔Street Story bridge key already existed (2026-10-10) in
`~/.local/state/rkb-street-story-poi/bridge.env` (0600), loaded into both services by the drop-ins
`99-rkb-poi-bridge.conf`; it is the canonical key, and the RKB delivery worker
(`regional-knowledge-street-story-delivery.service`) uses it too. The installer additionally reads
`~/.local/state/street-story/rkb-poi.json` (0600: `owner_subs`, and the same key aligned with the bridge)
into `STREET_STORY_RKB_OWNER_SUBS` on every deploy. First delivery 2026-10-19: the one pending book event
arrived and stayed `unresolved_identity` (name-only locator; a POI is created only from external ids).

**The owner's private books.** The owner granted delivery of their private book evidence to their own
Street Story (2026-10-19). Such evidence keeps `visibility=private` and its `owner_sub`; accepted claims
of the configured owner subjects enter that owner's stories like public ones. Other users' private
evidence never does. Street Story → RKB search during photo research is a later step.
