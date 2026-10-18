"""Exact unit receipt lookup; historical UNKNOWN is never a recent-N window."""
import hashlib
import json


def unit_attempt_rows(store, story, unit_id, *, roles=None, legacy_binding=False):
    generation = story.get('_identity_generation', 0)
    with store.connection() as db:
        if roles is None:
            # Read role metadata only, including historical review model roles.
            roles = tuple(row[0] for row in db.execute(
                "SELECT DISTINCT role FROM research_provider_attempts "
                "WHERE story_id=? AND role>='facts' AND role<'factt'", (story['id'],)))
        if not roles:
            return []
        logicals = [hashlib.sha256(json.dumps(
            [story['id'], story['photo_sha256'], generation, role, unit_id],
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            for role in roles]
        placeholders = ','.join('?' for _ in roles)
        fields = 'logical_id,role,receipt_json,created_at,rowid AS receipt_rowid'
        query = f'SELECT {fields} FROM research_provider_attempts WHERE logical_id IN ({placeholders})'
        args = list(logicals)
        if legacy_binding:
            query += (f' UNION SELECT {fields} FROM research_provider_attempts '
                      f'WHERE story_id=? AND role IN ({placeholders}) '
                      "AND json_extract(receipt_json,'$.binding.fact_unit_id')=? "
                      "AND json_extract(receipt_json,'$.binding.photo_sha256')=? "
                      "AND json_extract(receipt_json,'$.binding.generation')=?")
            args.extend((story['id'], *roles, unit_id, story['photo_sha256'], generation))
        query += ' ORDER BY created_at DESC,receipt_rowid DESC'
        return list(db.execute(query, args))
