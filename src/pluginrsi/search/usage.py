"""Rebuild per-iteration usage from compact API events and persisted attempts."""

from datetime import datetime, timezone
from pathlib import Path
import json

from .store import read_json, write_json

ROLES = ('solver', 'evolver')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens')
PHASES = ('plugin_mutation', 'harness_recomposition')


def collect_iteration_usage(config, store):
    models = {'solver': config.solver, 'evolver': config.proposer.agent}

    def stats(role):
        return dict(model=models[role].model if models[role] else None, input_tokens=0,
                    output_tokens=0, total_tokens=0, rollouts=0, api_requests=0,
                    api_failures=0, requests_without_usage=0, pending_requests=0)

    def row():
        return {role: stats(role) for role in ROLES}

    buckets = {name: row() for name in ('initialization', 'heldout', 'unattributed')}
    totals = row()
    completed = {}
    candidates, evaluations = {}, {}
    legacy_proposals = 0

    def bucket(iteration, phase):
        if phase == 'heldout':
            return 'heldout'
        if iteration == 0 or phase == 'seed':
            return 'initialization'
        if isinstance(iteration, int) and iteration > 0:
            buckets.setdefault(iteration, row())
            return iteration
        return 'unattributed'

    def add(where, role, field, value):
        buckets[where][role][field] += value
        totals[role][field] += value

    for path in sorted((store.root/'iterations').glob('iter_*/*.json')):
        record = read_json(path)
        iteration = record['iteration']
        where = bucket(iteration, record['phase'])
        completed.setdefault(iteration, {})[record['phase']] = bool(record['completed'])
        for slot in record['slots']:
            candidates[slot['id']] = where
            if slot.get('evaluation_id'):
                evaluations[slot['evaluation_id']] = where
            attempts = slot.get('proposal_attempts')
            if attempts is None:
                attempts = int((store.candidate(slot['id'])/'proposal_request.json').exists())
                legacy_proposals += attempts
            add(where, 'evolver', 'rollouts', attempts)
        for eid in record.get('parent_evaluations', {}).values():
            evaluations[eid] = where

    for key, eid in store.state.get('evaluation_keys', {}).items():
        if key == 'seed:selection':
            evaluations[eid] = 'initialization'
        elif key.startswith('final:heldout:'):
            evaluations[eid] = 'heldout'
        elif key.startswith('local:') and key.endswith(':parent'):
            evaluations[eid] = bucket(int(key.split(':')[1]), 'plugin_mutation')

    for path in (store.root/'evaluations').glob('*/evaluation.json'):
        record = read_json(path)
        where = bucket(record.get('iteration'), record.get('phase')) if 'iteration' in record else evaluations.get(record['id'], 'unattributed')
        if record.get('purpose') == 'heldout':
            where = 'heldout'
        evaluations[record['id']] = where
        add(where, 'solver', 'rollouts', record.get('rollouts_started', 0))
    discrepancy = store.state.get('rollouts_started', 0) - totals['solver']['rollouts']
    if discrepancy > 0:
        add('unattributed', 'solver', 'rollouts', discrepancy)

    paths = {}
    for role, model in models.items():
        if model and model.api_metrics_path:
            paths.setdefault(Path(model.api_metrics_path), set()).add(role)
    requests = {}
    for path, roles in paths.items():
        if not path.exists():
            continue
        with path.open() as stream:
            for line in stream:
                if not line.endswith('\n'):
                    break
                event = json.loads(line)
                if event.get('event') not in ('api/request_start', 'api/request_end'):
                    continue
                role = event.get('model_role')
                if role not in ROLES:
                    role = next(iter(roles)) if len(roles) == 1 else ('evolver' if event.get('producer', {}).get('kind') == 'proposer' else 'solver')
                requests.setdefault((role, event['request_id']), {})[event['event']] = event

    for (role, _), events in requests.items():
        end = events.get('api/request_end')
        event = end or events['api/request_start']
        where = evaluations.get(event.get('evaluation_id')) if role == 'solver' else candidates.get(event.get('candidate_id'))
        if where is None:
            where = bucket(event.get('iteration'), event.get('phase'))
        add(where, role, 'api_requests', 1)
        usage = (end or {}).get('usage') or {}
        for field in TOKEN_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                add(where, role, field, value)
        if any(usage.get(field) is None for field in TOKEN_FIELDS):
            add(where, role, 'requests_without_usage', 1)
        if end is None:
            add(where, role, 'pending_requests', 1)
        elif not end['success']:
            add(where, role, 'api_failures', 1)

    return dict(schema_version=1, updated_at=datetime.now(timezone.utc).isoformat(),
        rollout_units={'solver': 'task attempts including startup failures and retries',
                       'evolver': 'proposal invocations including failed proposals and retries'},
        token_totals='reported API usage only; missing usage is counted separately',
        metrics_enabled={role: bool(model and model.api_metrics_path) for role, model in models.items()},
        legacy_proposals_with_unknown_retry_count=legacy_proposals,
        task_rollout_record_discrepancy=discrepancy,
        initialization=buckets['initialization'], heldout=buckets['heldout'],
        unattributed=buckets['unattributed'],
        iterations=[dict(iteration=i, completed=all(completed.get(i, {}).get(p, False) for p in PHASES),
                         completed_phases=completed.get(i, {}), **buckets[i])
                    for i in sorted(k for k in buckets if isinstance(k, int))], totals=totals)


def write_iteration_usage(config, store):
    report = collect_iteration_usage(config, store)
    write_json(store.root/'iteration_usage.json', report)
    return report
