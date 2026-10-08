from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS stories(
  id TEXT PRIMARY KEY,
  client_story_id TEXT NOT NULL UNIQUE,
  photo_sha256 TEXT NOT NULL,
  photo_mime_type TEXT NOT NULL,
  photo_path TEXT NOT NULL,
  latitude REAL,
  longitude REAL,
  voice_protocol TEXT NOT NULL,
  state TEXT NOT NULL,
  place_name TEXT,
  summary TEXT,
  draft_text TEXT,
  processed_image_path TEXT,
  processed_image_url TEXT,
  vibepublish_asset_ref TEXT,
  scheduled_for TEXT,
  published_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  error_message TEXT,
  research_json TEXT NOT NULL DEFAULT '{}',
  visual_context_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_sessions(
  session_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  open_digest TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  recording_finished INTEGER NOT NULL DEFAULT 0,
  manifest_json TEXT,
  transcript TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_chunks(
  session_id TEXT NOT NULL REFERENCES voice_sessions(session_id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  wall_start_ms INTEGER NOT NULL,
  wall_end_ms INTEGER NOT NULL,
  mime_type TEXT NOT NULL,
  transcript TEXT,
  PRIMARY KEY(session_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS facts(
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_supported INTEGER NOT NULL,
  selected INTEGER NOT NULL,
  sources_json TEXT NOT NULL,
  PRIMARY KEY(story_id, fact_id)
);
CREATE TABLE IF NOT EXISTS live_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  message_key TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  text TEXT NOT NULL,
  final INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(story_id,message_key)
);
CREATE INDEX IF NOT EXISTS idx_live_messages_story_id
  ON live_messages(story_id,id);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  semantic_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at REAL NOT NULL,
  lease_until REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, available_at, lease_until, created_at);
CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_accepted_reference_candidate
 ON stories(json_extract(research_json,'$.visual_identity.candidate_id'),updated_at DESC)
 WHERE json_extract(research_json,'$.visual_identity.status')='match'
 AND json_extract(research_json,'$.visual_identity.visual_reference_verified')=1;
CREATE TABLE IF NOT EXISTS publish_intents(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  request_key TEXT NOT NULL UNIQUE,
  vibepublish_request_key TEXT NOT NULL UNIQUE,
  request_json TEXT NOT NULL,
  vibepublish_operation_id TEXT,
  state TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""


# Additive schema, applied to existing WAL stores without replacing business rows.
RELIABILITY_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS gemini_credentials(
 key_id TEXT PRIMARY KEY, disabled INTEGER NOT NULL DEFAULT 0,
 disabled_reason TEXT, busy_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gemini_key_health(
 key_id TEXT NOT NULL REFERENCES gemini_credentials(key_id),
 model TEXT NOT NULL, operation TEXT NOT NULL,
 cooldown_until REAL NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
 last_success REAL NOT NULL DEFAULT 0, last_selected REAL NOT NULL DEFAULT 0,
 quota_state TEXT NOT NULL DEFAULT 'available', retry_after REAL, last_failure TEXT,
 minute_bucket INTEGER NOT NULL DEFAULT 0, minute_used INTEGER NOT NULL DEFAULT 0,
 advisory_until REAL NOT NULL DEFAULT 0, advisory_load REAL NOT NULL DEFAULT 0, advisory_observed_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(key_id,model,operation)
);
CREATE TABLE IF NOT EXISTS gemini_model_blocks(
 key_id TEXT NOT NULL REFERENCES gemini_credentials(key_id),
 model TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(key_id,model)
);
CREATE TABLE IF NOT EXISTS gemini_quota_journal(
 request_uid TEXT PRIMARY KEY, state TEXT NOT NULL, deadline REAL NOT NULL,
 finalize_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_checkpoints(
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 stage TEXT NOT NULL, value_json TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(job_id,stage)
);
CREATE TABLE IF NOT EXISTS fact_conflicts(
 story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
 conflict_id TEXT NOT NULL,
 poi_key TEXT,
 left_fact_id TEXT NOT NULL,
 right_fact_id TEXT NOT NULL,
 left_text TEXT NOT NULL,
 right_text TEXT NOT NULL,
 relation TEXT NOT NULL,
 detector_confidence REAL NOT NULL,
 suggested_resolution TEXT NOT NULL,
 suggested_fact_id TEXT,
 detector_rationale TEXT NOT NULL,
 final_resolution TEXT,
 final_fact_id TEXT,
 arbitration_reason TEXT,
 arbitration_confidence REAL,
 arbitrated_by TEXT,
 evidence_json TEXT NOT NULL,
 left_revision_digest TEXT NOT NULL DEFAULT '',
 right_revision_digest TEXT NOT NULL DEFAULT '',
 times_seen INTEGER NOT NULL DEFAULT 1,
 first_seen_at REAL NOT NULL,
 last_seen_at REAL NOT NULL,
 PRIMARY KEY(story_id,conflict_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_conflicts_story_open
 ON fact_conflicts(story_id,final_resolution,last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_fact_conflicts_poi_relation
 ON fact_conflicts(poi_key,relation,last_seen_at DESC);
CREATE TABLE IF NOT EXISTS fact_conflict_scans(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
 poi_key TEXT,
 run_id TEXT,
 detector TEXT NOT NULL,
 status TEXT NOT NULL,
 pair_count INTEGER NOT NULL,
 detected_count INTEGER NOT NULL,
 coverage_complete INTEGER NOT NULL DEFAULT 0,
 revision_bundle_json TEXT NOT NULL DEFAULT '{}',
 conflict_ids_json TEXT NOT NULL DEFAULT '[]',
 missing_aspects_json TEXT NOT NULL DEFAULT '[]',
 error_type TEXT,
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_conflict_scans_story_time
 ON fact_conflict_scans(story_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fact_conflict_scans_poi_time
 ON fact_conflict_scans(poi_key,created_at DESC);

CREATE TABLE IF NOT EXISTS fact_arbitration_events(
  event_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  conflict_id TEXT NOT NULL,
  resolution TEXT NOT NULL,
  final_fact_id TEXT,
  reason TEXT NOT NULL,
  confidence REAL NOT NULL,
  arbitrated_by TEXT NOT NULL,
  left_revision_digest TEXT NOT NULL,
  right_revision_digest TEXT NOT NULL,
  evidence_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','stale','superseded')),
  stale_reason TEXT,
  created_at REAL NOT NULL,
  stale_at REAL
);
CREATE INDEX IF NOT EXISTS idx_fact_arbitration_events_conflict
 ON fact_arbitration_events(story_id,conflict_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fact_arbitration_events_state
 ON fact_arbitration_events(story_id,state,created_at DESC);

CREATE TABLE IF NOT EXISTS fact_relation_events(
  event_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  incoming_index INTEGER NOT NULL,
  incoming_text TEXT NOT NULL,
  incoming_text_sha256 TEXT NOT NULL,
  existing_fact_id TEXT NOT NULL,
  existing_revision_digest TEXT NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN (
    'equivalent','different','contradicts','refines',
    'temporal_sequence','scope_difference','uncertain'
  )),
  model_name TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  rationale TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_relation_events_story_run
 ON fact_relation_events(story_id,run_id,created_at);
CREATE INDEX IF NOT EXISTS idx_fact_relation_events_existing
 ON fact_relation_events(story_id,existing_fact_id,created_at DESC);

CREATE TABLE IF NOT EXISTS fact_assertions(
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  assertion_id TEXT NOT NULL,
  semantic_key TEXT,
  display_text TEXT NOT NULL,
  owner_selected INTEGER NOT NULL DEFAULT 0,
  review_status TEXT NOT NULL DEFAULT 'unreviewed'
    CHECK(review_status IN ('unreviewed','eligible','disputed','withheld','quarantined')),
  eligibility TEXT NOT NULL DEFAULT 'unreviewed'
    CHECK(eligibility IN ('unreviewed','eligible','withheld')),
  revision_digest TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(story_id,assertion_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_assertions_story_review
 ON fact_assertions(story_id,eligibility,owner_selected);

CREATE TABLE IF NOT EXISTS fact_observations(
  observation_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  poi_key TEXT,
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  assertion_id TEXT NOT NULL,
  model_claim_key TEXT,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('candidate','accepted','quarantined','retracted')),
  structural_error TEXT,
  model_name TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_observations_story_assertion
 ON fact_observations(story_id,assertion_id,created_at);
CREATE INDEX IF NOT EXISTS idx_fact_observations_poi_time
 ON fact_observations(poi_key,created_at DESC);

CREATE TABLE IF NOT EXISTS fact_evidence_spans(
  evidence_id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES fact_observations(observation_id) ON DELETE CASCADE,
  source_url TEXT NOT NULL,
  source_version_id TEXT NOT NULL,
  support_kind TEXT NOT NULL,
  span_text TEXT NOT NULL,
  span_sha256 TEXT NOT NULL,
  chunk_id TEXT,
  span_start INTEGER,
  span_end INTEGER,
  relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','partial')),
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_evidence_observation
 ON fact_evidence_spans(observation_id,source_version_id);
CREATE INDEX IF NOT EXISTS idx_fact_evidence_source_version
 ON fact_evidence_spans(source_version_id,evidence_id);

CREATE TABLE IF NOT EXISTS research_runs(
  run_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  poi_key TEXT,
  goal TEXT NOT NULL,
  extraction_scope TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK(state IN (
    'planned','discovering','fetching','extracting','reconciling','verifying',
    'completed','partial','failed','cancelled'
  )),
  status_detail TEXT NOT NULL DEFAULT '',
  expected_story_revision INTEGER NOT NULL,
  identity_generation INTEGER NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_research_runs_story_time
 ON research_runs(story_id,created_at DESC);

CREATE TABLE IF NOT EXISTS research_provider_attempts(
  attempt_id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  receipt_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_provider_logical ON research_provider_attempts(logical_id,created_at);
CREATE INDEX IF NOT EXISTS idx_research_provider_role_time ON research_provider_attempts(role,updated_at);

CREATE TABLE IF NOT EXISTS source_documents(
  document_id TEXT PRIMARY KEY,
  canonical_url TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source_versions(
  source_version_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
  requested_url TEXT NOT NULL,
  final_url TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  content_type TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  redirect_chain_json TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  read_status TEXT NOT NULL,
  access_scope TEXT NOT NULL DEFAULT 'story',
  char_count INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_versions_document_time
 ON source_versions(document_id,created_at DESC);

CREATE TABLE IF NOT EXISTS source_chunks(
  chunk_id TEXT PRIMARY KEY,
  source_version_id TEXT NOT NULL REFERENCES source_versions(source_version_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  core_start INTEGER NOT NULL,
  core_end INTEGER NOT NULL,
  context_start INTEGER NOT NULL,
  context_end INTEGER NOT NULL,
  chunk_text TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(source_version_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_source_chunks_version_ordinal
 ON source_chunks(source_version_id,ordinal);

CREATE TABLE IF NOT EXISTS research_run_sources(
  run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'discovered','snippet_only','fetching','fetched','partial','failed','deferred'
  )),
  source_version_id TEXT REFERENCES source_versions(source_version_id),
  error_code TEXT,
  discovered_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(run_id,url)
);
CREATE INDEX IF NOT EXISTS idx_research_run_sources_state
 ON research_run_sources(run_id,status,updated_at);

CREATE TABLE IF NOT EXISTS research_chunk_runs(
  run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
  chunk_id TEXT NOT NULL REFERENCES source_chunks(chunk_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN (
    'planned','extracting','extracted','no_claims','needs_context','failed','deferred','cancelled'
  )),
  observation_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  model_name TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  reuse_from_run_id TEXT,
  reuse_kind TEXT NOT NULL DEFAULT '',
  lease_owner TEXT,
  lease_until REAL NOT NULL DEFAULT 0,
  lease_fence INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY(run_id,chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_research_chunk_runs_state
 ON research_chunk_runs(run_id,status,updated_at);

CREATE TABLE IF NOT EXISTS research_chunk_batches(
  batch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
  chunk_id TEXT NOT NULL REFERENCES source_chunks(chunk_id) ON DELETE CASCADE,
  batch_index INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('completed','continuation','failed','deferred')),
  raw_fact_count INTEGER NOT NULL DEFAULT 0,
  accepted_fact_count INTEGER NOT NULL DEFAULT 0,
  continuation_needed INTEGER NOT NULL DEFAULT 0,
  continuation_reason TEXT NOT NULL DEFAULT '',
  error_code TEXT,
  model_name TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '',
  payload_sha256 TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(run_id,chunk_id,batch_index)
);
CREATE INDEX IF NOT EXISTS idx_research_chunk_batches_run_chunk
 ON research_chunk_batches(run_id,chunk_id,batch_index);
"""



POI_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS pois(
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('candidate','verified','merged')),
  canonical_name TEXT NOT NULL,
  latitude REAL,
  longitude REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS poi_aliases(
  poi_id TEXT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
  namespace TEXT NOT NULL,
  value TEXT NOT NULL,
  normalized_value TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(poi_id,namespace,normalized_value),
  UNIQUE(namespace,normalized_value)
);
CREATE INDEX IF NOT EXISTS idx_poi_aliases_poi
 ON poi_aliases(poi_id,namespace);

CREATE TABLE IF NOT EXISTS poi_research_facts(
  poi_key TEXT NOT NULL,
  claim_key TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  sources_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(poi_key,claim_key)
);
CREATE INDEX IF NOT EXISTS idx_poi_research_facts_recent
 ON poi_research_facts(poi_key,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_research_assertions(
  poi_key TEXT NOT NULL,
  assertion_id TEXT NOT NULL,
  semantic_key TEXT,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  sources_json TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'unreviewed'
    CHECK(review_status IN ('unreviewed','eligible','disputed','withheld','quarantined')),
  eligibility TEXT NOT NULL DEFAULT 'unreviewed'
    CHECK(eligibility IN ('unreviewed','eligible','withheld')),
  review_story_id TEXT,
  reviewed_at REAL,
  review_proof_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(poi_key,assertion_id)
);
CREATE INDEX IF NOT EXISTS idx_poi_research_assertions_semantic
 ON poi_research_assertions(poi_key,semantic_key,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_poi_research_assertions_recent
 ON poi_research_assertions(poi_key,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_research_observations(
  observation_id TEXT PRIMARY KEY,
  poi_key TEXT NOT NULL,
  assertion_id TEXT NOT NULL,
  semantic_key TEXT,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  sources_json TEXT NOT NULL,
  research_run_id TEXT,
  query TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poi_research_observations_assertion
 ON poi_research_observations(poi_key,assertion_id,created_at DESC);

CREATE TABLE IF NOT EXISTS poi_research_sources(
  poi_key TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  supports_json TEXT NOT NULL,
  last_query TEXT NOT NULL,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  PRIMARY KEY(poi_key,url)
);
CREATE INDEX IF NOT EXISTS idx_poi_research_sources_recent
 ON poi_research_sources(poi_key,last_seen_at DESC);

CREATE TABLE IF NOT EXISTS poi_external_events(
  event_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  producer TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK(visibility IN ('private','workspace','public')),
  owner_sub TEXT,
  workspace_id TEXT,
  source_ref TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  candidate_id TEXT NOT NULL,
  poi_id TEXT REFERENCES pois(id) ON DELETE SET NULL,
  claim_id TEXT,
  state TEXT NOT NULL CHECK(state IN (
    'unresolved_identity','attached','rejected','superseded'
  )),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poi_external_events_scope
 ON poi_external_events(visibility,owner_sub,workspace_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_poi_external_events_poi
 ON poi_external_events(poi_id,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_claims(
  id TEXT PRIMARY KEY,
  poi_id TEXT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
  semantic_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK(status IN ('candidate','accepted','contested','rejected')),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(poi_id,semantic_key)
);
CREATE INDEX IF NOT EXISTS idx_poi_claims_poi_kind
 ON poi_claims(poi_id,kind,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_claim_evidence(
  claim_id TEXT NOT NULL REFERENCES poi_claims(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES poi_external_events(event_id) ON DELETE CASCADE,
  evidence_ref TEXT NOT NULL,
  source_family_id TEXT NOT NULL,
  author_score INTEGER,
  publication_score INTEGER,
  provenance_score INTEGER NOT NULL,
  verification_score INTEGER,
  evidence_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(claim_id,event_id),
  CHECK(author_score IS NULL OR author_score BETWEEN 0 AND 100),
  CHECK(publication_score IS NULL OR publication_score BETWEEN 0 AND 100),
  CHECK(provenance_score BETWEEN 0 AND 100),
  CHECK(verification_score IS NULL OR verification_score BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS poi_conflicts(
  conflict_id TEXT PRIMARY KEY,
  poi_id TEXT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
  left_claim_id TEXT NOT NULL REFERENCES poi_claims(id) ON DELETE CASCADE,
  right_claim_id TEXT NOT NULL REFERENCES poi_claims(id) ON DELETE CASCADE,
  relation TEXT NOT NULL DEFAULT 'uncertain'
    CHECK(relation IN (
      'contradiction','scope_difference','temporal_sequence',
      'source_disagreement','uncertain'
    )),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','resolved','superseded')),
  times_seen INTEGER NOT NULL DEFAULT 1,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  UNIQUE(poi_id,left_claim_id,right_claim_id)
);
CREATE INDEX IF NOT EXISTS idx_poi_conflicts_open
 ON poi_conflicts(poi_id,status,last_seen_at DESC);

CREATE TABLE IF NOT EXISTS poi_media_events(
  event_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  producer TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK(visibility IN ('private','workspace','public')),
  owner_sub TEXT,
  workspace_id TEXT,
  source_ref TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  poi_id TEXT REFERENCES pois(id) ON DELETE SET NULL,
  illustration_ref TEXT NOT NULL,
  illustration_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN (
    'depicts','illustrates','map_of','detail_of'
  )),
  time_scope TEXT,
  media_kind TEXT NOT NULL,
  caption TEXT,
  page_id TEXT NOT NULL,
  source_region_id TEXT NOT NULL,
  source_crop_sha256 TEXT NOT NULL,
  rights_status TEXT NOT NULL,
  vibepublish_entry_ref TEXT,
  state TEXT NOT NULL CHECK(state IN (
    'unresolved_identity','attached','rejected','superseded'
  )),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poi_media_events_poi
 ON poi_media_events(poi_id,visibility,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_poi_media_events_scope
 ON poi_media_events(visibility,owner_sub,workspace_id,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_review_cases(
  review_case_id TEXT PRIMARY KEY,
  conflict_id TEXT NOT NULL UNIQUE
    REFERENCES poi_conflicts(conflict_id) ON DELETE CASCADE,
  poi_id TEXT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
  relation TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN (
      'open','assigned','in_review','resolved','deferred','superseded'
    )),
  required_expertise_json TEXT NOT NULL,
  required_reviews INTEGER NOT NULL DEFAULT 1
    CHECK(required_reviews BETWEEN 1 AND 3),
  scope_json TEXT NOT NULL,
  detector_suggestion_json TEXT,
  case_revision INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poi_review_cases_status
 ON poi_review_cases(status,updated_at DESC);

CREATE TABLE IF NOT EXISTS poi_review_assignments(
  review_case_id TEXT NOT NULL
    REFERENCES poi_review_cases(review_case_id) ON DELETE CASCADE,
  expert_sub TEXT NOT NULL,
  assignment_revision INTEGER NOT NULL,
  expertise_snapshot_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'assigned'
    CHECK(state IN ('assigned','accepted','submitted','revoked')),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(review_case_id,expert_sub,assignment_revision)
);

CREATE TABLE IF NOT EXISTS poi_review_decisions(
  decision_id TEXT PRIMARY KEY,
  review_case_id TEXT NOT NULL
    REFERENCES poi_review_cases(review_case_id) ON DELETE CASCADE,
  expert_sub TEXT NOT NULL,
  assignment_revision INTEGER NOT NULL,
  case_revision_observed INTEGER NOT NULL,
  resolution TEXT NOT NULL CHECK(resolution IN (
    'prefer_left','prefer_right','both_valid_scope','both_valid_temporal',
    'unresolved','needs_more_sources','wrong_poi_link'
  )),
  rationale TEXT NOT NULL,
  confidence REAL,
  command_digest TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(review_case_id,expert_sub,assignment_revision)
);

CREATE TABLE IF NOT EXISTS poi_semantic_candidates(
  candidate_id TEXT PRIMARY KEY,
  poi_id TEXT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
  left_claim_id TEXT NOT NULL REFERENCES poi_claims(id) ON DELETE CASCADE,
  right_claim_id TEXT NOT NULL REFERENCES poi_claims(id) ON DELETE CASCADE,
  scope_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending_model'
    CHECK(state IN (
      'pending_model','confirmed_conflict','confirmed_no_conflict','superseded'
    )),
  analysis_digest TEXT,
  analysis_json TEXT,
  times_seen INTEGER NOT NULL DEFAULT 1,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  UNIQUE(poi_id,left_claim_id,right_claim_id)
);
CREATE INDEX IF NOT EXISTS idx_poi_semantic_candidates_pending
 ON poi_semantic_candidates(state,last_seen_at DESC);

CREATE TABLE IF NOT EXISTS poi_review_commands(
  command_id TEXT PRIMARY KEY,
  review_case_id TEXT NOT NULL
    REFERENCES poi_review_cases(review_case_id) ON DELETE CASCADE,
  actor_sub TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('accept','resolve','request_research')),
  payload_digest TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poi_review_commands_case
 ON poi_review_commands(review_case_id,created_at DESC);
"""


class ScopedConnection(sqlite3.Connection):
    """sqlite3's standard context commits/rolls back but does not close its FD."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            db.executescript(RELIABILITY_SCHEMA)
            db.executescript(POI_SCHEMA)
            for table, name, declaration in (
                ('research_runs', 'extraction_scope', "TEXT NOT NULL DEFAULT ''"),
                ('source_versions', 'access_scope', "TEXT NOT NULL DEFAULT 'story'"),
            ):
                columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                if name not in columns:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
            scan_columns = {row[1] for row in db.execute("PRAGMA table_info(fact_conflict_scans)")}
            for name, sql in (
                ("coverage_complete", "INTEGER NOT NULL DEFAULT 0"),
                ("run_id", "TEXT"),
                ("revision_bundle_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("conflict_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("missing_aspects_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in scan_columns:
                    db.execute(
                        f"ALTER TABLE fact_conflict_scans ADD COLUMN {name} {sql}"
                    )
            conflict_columns = {row[1] for row in db.execute("PRAGMA table_info(fact_conflicts)")}
            for name in ("left_revision_digest", "right_revision_digest"):
                if name not in conflict_columns:
                    db.execute(
                        f"ALTER TABLE fact_conflicts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            batch_columns = {
                row[1] for row in db.execute("PRAGMA table_info(research_chunk_batches)")
            }
            chunk_columns = {row[1] for row in db.execute('PRAGMA table_info(research_chunk_runs)')}
            for name, sql in (("reuse_from_run_id", "TEXT"), ("reuse_kind", "TEXT NOT NULL DEFAULT ''"),
                              ("lease_owner", "TEXT"), ("lease_until", "REAL NOT NULL DEFAULT 0"),
                              ("lease_fence", "INTEGER NOT NULL DEFAULT 0")):
                if name not in chunk_columns:
                    db.execute(f'ALTER TABLE research_chunk_runs ADD COLUMN {name} {sql}')
            for name in ("payload_json", "payload_sha256"):
                if name not in batch_columns:
                    db.execute(
                        f"ALTER TABLE research_chunk_batches "
                        f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            evidence_columns = {row[1] for row in db.execute("PRAGMA table_info(fact_evidence_spans)")}
            for name, sql_type in (
                ("chunk_id", "TEXT"),
                ("span_start", "INTEGER"),
                ("span_end", "INTEGER"),
            ):
                if name not in evidence_columns:
                    db.execute(f"ALTER TABLE fact_evidence_spans ADD COLUMN {name} {sql_type}")
            from .fact_ledger import (
                backfill_legacy_fact_ledger,
                repair_missing_evidence_edges,
            )
            now = self.now()
            backfill_legacy_fact_ledger(db, now)
            repair_missing_evidence_edges(db, now)
            poi_assertion_columns = {
                row[1] for row in db.execute("PRAGMA table_info(poi_research_assertions)")
            }
            for name, sql in (
                ("review_status", "TEXT NOT NULL DEFAULT 'unreviewed'"),
                ("eligibility", "TEXT NOT NULL DEFAULT 'unreviewed'"),
                ("review_story_id", "TEXT"),
                ("reviewed_at", "REAL"),
                ("review_proof_json", "TEXT"),
            ):
                if name not in poi_assertion_columns:
                    db.execute(
                        f"ALTER TABLE poi_research_assertions ADD COLUMN {name} {sql}"
                    )
            from .poi_memory import (
                backfill_legacy_research_memory,
                backfill_poi_assertion_review_state,
                backfill_poi_review_proofs,
            )
            now = self.now()
            backfill_legacy_research_memory(db, now)
            backfill_poi_assertion_review_state(db, now)
            backfill_poi_review_proofs(db)

    def connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ScopedConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = self.connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def now() -> float:
        return time.time()

    def cache_get(self, key: str) -> Any | None:
        with self.connection() as db:
            row = db.execute("SELECT value_json,expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row or row["expires_at"] <= self.now():
            return None
        return json.loads(row["value_json"])

    def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None:
        now = self.now()
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.tx() as db:
            db.execute(
                "INSERT INTO cache(key,value_json,expires_at,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at,created_at=excluded.created_at",
                (key, raw, now + ttl_seconds, now),
            )

    def checkpoint_get(self, job_id: str, stage: str) -> Any | None:
        with self.connection() as db:
            row = db.execute("SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage=?", (job_id, stage)).fetchone()
        return json.loads(row[0]) if row else None

    def checkpoint_put(self, job_id: str, stage: str, value: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?)",
                       (job_id, stage, json.dumps(value, ensure_ascii=False), self.now()))
