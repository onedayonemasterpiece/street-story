"""Resume model-proposed own-evidence replacements through the public repair tool."""
import json

from jsonschema import Draft202012Validator

from . import headless_review_quotes, review_packets
from .service import ConflictError


REPLACEMENTS_SCHEMA = {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {
    'type': 'object', 'properties': {
        'text': {'type': 'string', 'minLength': 1, 'maxLength': 500},
        'claim_key': {'type': 'string', 'minLength': 1, 'maxLength': 300},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                     'items': {'type': 'integer', 'minimum': 0}},
    }, 'required': ['text', 'claim_key', 'evidence'], 'additionalProperties': False}}


async def resume(engine, session, job, unit, outcome):
    """No provider operation here; replacement eligibility needs a fresh review."""
    state = outcome.get('repair_state') or {}
    if state.get('phase') in {'applied', 'rejected', 'stale', 'none'}:
        return
    args = outcome.get('args') or {}
    try:
        with engine.service.store.connection() as db:
            row = db.execute('SELECT payload_json,run_id FROM live_review_packets WHERE packet_ref=?',
                (outcome['packet_ref'],)).fetchone()
            original = json.loads(row['payload_json'])
            story = engine.service._story_row(db, job['story_id'])
            if (row['run_id'] != session.state['research_run_id'] or original.get('owner_fence') !=
                    review_packets.candidate_review_fence(db, story)):
                raise ConflictError('live_review_packet_stale', 'Original repair owner inputs changed.')
        if not state:
            proposals = []
            resolved = headless_review_quotes.resolve_rows(outcome['frozen_packet'], args)
            for index, decision in enumerate(args.get('decisions', [])):
                if not isinstance(decision, dict) or not decision.get('repair_facts'):
                    continue
                # Fresh private proposals use the same scoped quote address as
                # their parent review. Legacy frozen proposals keep exact indices.
                decision = headless_review_quotes.resolve_quotes(outcome['frozen_packet'],
                    {**args, 'decisions': [decision]}, include_repairs=True)['decisions'][0]
                checked = {key: value for key, value in resolved['decisions'][index].items()
                           if key not in {'repair_facts', 'own_evidence_values', 'own_value_conflicts'}}
                review_packets.validate_decision(checked, original, outcome['packet_ref'])
                if decision.get('verdict') != 'repair_needed':
                    raise ConflictError('live_research_fact_invalid', 'Replacements require the original repair_needed verdict.')
                if list(Draft202012Validator(REPLACEMENTS_SCHEMA).iter_errors(decision['repair_facts'])):
                    raise ConflictError('live_research_fact_invalid', 'Malformed model-owned replacements.')
                item = original['items'][decision['fact']]
                if len(decision['repair_facts']) == 1 and decision['repair_facts'][0]['text'] == item['text']:
                    raise ConflictError('live_research_repair_no_change',
                        'The closed repair proposal adds no meaning or new own context.')
                proposals.append((decision, item))
            if not proposals:
                engine._put(job, unit, {**outcome, 'repair_state': {'phase': 'none'}})
                return
            packet = review_packets.read(engine.harness.adapter, session, {
                'run_id': session.state['research_run_id'],
                'fact_ids': [item['id'] for _, item in proposals], '_parallel_candidate_review': True})
            with engine.service.store.connection() as db:
                fresh = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                    (packet['packet_ref'],)).fetchone()[0])
            mapped = {item['id']: (n, item) for n, item in enumerate(fresh['items'])}
            repairs = []
            for decision, item in proposals:
                number, current = mapped[item['id']]
                own = {(ev['id'], ev['sha']): n for n, ev in enumerate(current['evidence'])}
                children = []
                for child in decision['repair_facts']:
                    indices = child['evidence']
                    if any(type(n) is not int or not 0 <= n < len(item['evidence']) for n in indices):
                        raise ConflictError('live_fact_review_evidence_invalid', 'Replacement must use original own evidence.')
                    refs = [(item['evidence'][n]['id'], item['evidence'][n]['sha']) for n in indices]
                    if any(ref not in own for ref in refs):
                        raise ConflictError('live_review_packet_stale', 'Original repair evidence changed.')
                    children.append({**child, 'evidence': [own[ref] for ref in refs]})
                repairs.append({'fact': number, 'reason': decision['reason'], 'facts': children})
            state = {'phase': 'prepared', 'args': {'packet_ref': packet['packet_ref'], 'repairs': repairs}}
            # Durable before mutation; command replay recovers a lost acknowledgement.
            engine._put(job, unit, {**outcome, 'repair_state': state})
        value = await engine.harness.adapter.execute_tool(session, {
            'name': 'repair_research_fact', 'id': 'background-repair-' + unit, 'args': state['args']})
        engine._put(job, unit, {**outcome, 'repair_state': {**state, 'phase': 'applied', 'result': value}})
    except ConflictError as exc:
        engine._put(job, unit, {**outcome, 'repair_state': {**state,
            'phase': 'stale' if exc.code.endswith('stale') else 'rejected', 'error_code': exc.code}})
