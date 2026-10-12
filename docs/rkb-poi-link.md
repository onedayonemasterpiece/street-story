# Regional Knowledge Base → Street Story POI bridge

One model owns the decision: a physical/historically memorialized site is worth a POI, independent of whether its original building still stands. A location can be larger than a POI, e.g. island, district or street; never create a POI per source chunk.

RKB stages source-backed locations/persons/events/stories first. The model calls RKB `poi_registry(search)` for physical candidates. It explicitly `select`s the existing Street Story POI or `create`s a new `candidate` site (standing / ruins / memorial / lost_site / historic_site), attaching that canonical owner ID to the SAME RKB entity.

The RKB model does not insert rows into Street Story DB. Its authenticated MCP calls Street Story `POST /v1/internal/rkb-poi` over fixed loopback with an independent scoped service credential. The owner uses the existing canonical `pois` registry and a small `rkb_poi_links` table for the inverse source-identity pointer; no source text is replicated. After owner receipt, RKB updates its external_ref. A lost response is recovered by stable idempotency, not a second POI.

Only `search` suggests matches. `select` checks the owner ID exists, `create` rejects unreviewed same-name/external-ID candidates; the model, not the backend, evaluates site equivalence. Creation does not verify historical facts or invent coordinates.

Street Story can read historical material from a POI using its device-authenticated `GET /v1/pois/{poi_id}/knowledge`. Backend then requests RKB bounded source context with a dedicated loopback grant, never forwarding the Android bearer or an end-user RKB OAuth token. RKB checks its actor's source ACL and returns entity ID, evidence pointers, directly linked stories and one-hop persons/events. Details are fetched using RKB evidence tools when necessary.

The service bridge is disabled unless privately configured. Existing Street Story `STREET_STORY_DEVICE_TOKEN` remains unchanged. Current source-ingestion contract: [RKB location registry](https://github.com/onedayonemasterpiece/regional-knowledge-base/blob/main/docs/location-registry.md).
