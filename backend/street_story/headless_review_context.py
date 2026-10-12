"""Read-only retained source context for an existing own-evidence packet.

Context can disambiguate roles and antecedents. It does not create support,
quotes, observations, accepted facts or repair references.
"""
import hashlib
import json

from .service import ConflictError


def source_context(db, packet_ref, story_id):
    row = db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=? AND story_id=?',
        (packet_ref, story_id)).fetchone()
    if row is None:
        raise ConflictError('live_review_packet_unknown', 'Context belongs to the same story review packet.')
    payload = json.loads(row['payload_json'])
    documents, bindings = {}, []
    for fact, item in enumerate(payload['items']):
        for number, evidence in enumerate(item['evidence']):
            version_id = evidence['source_version_id']
            if version_id not in documents:
                source = db.execute('SELECT source_version_id,content_sha256,normalized_text,final_url '
                    'FROM source_versions WHERE source_version_id=?', (version_id,)).fetchone()
                if source is None or not source['normalized_text']:
                    documents[version_id] = {'source_version_id': version_id, 'available': False,
                        'complete': False, 'attached_as_support': False}
                else:
                    text = source['normalized_text']
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    if digest != source['content_sha256']:
                        raise ConflictError('live_review_source_context_invalid', 'Retained source version changed.')
                    documents[version_id] = {'source_version_id': version_id, 'content_sha256': digest,
                        'source_url': source['final_url'], 'text': text, 'available': True,
                        'complete': True, 'attached_as_support': False}
            bindings.append({'fact': fact, 'evidence': number, 'source_version_id': version_id})
    return {'contract': 'own-retained-context-not-support-v1', 'bindings': bindings,
        'documents': list(documents.values()),
        'complete': all(d['complete'] for d in documents.values())}
