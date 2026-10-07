"""Bounded public evidence views; original traces remain available on demand."""

import difflib
import json
import os
from collections import Counter
from pathlib import Path

from .store import read_json, write_json

CACHE_VERSION = 1
COMMAND_CHARS = 1200
OUTPUT_CHARS = 1200
SUMMARY_CHARS = 240
DIFF_CHARS = 16000
GLOBAL_FAILURE_EXAMPLES = 3
GLOBAL_CONTROL_EXAMPLES = 1
BRIEF_KEYS = ("id", "parent", "branch", "step", "status", "reason", "mean_score", "parent_mean",
              "matched_gain", "accuracy_gain", "wins", "losses", "retained", "win_task_ids",
              "loss_task_ids")


def trace_views(path, score):
    path = Path(path)
    summary_path = path.with_name("proposer_compact_summary.json")
    events_path = path.with_name("proposer_events.jsonl")
    stat = path.stat()
    signature = dict(version=CACHE_VERSION, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                     task_id=score['task_id'], score=score['score'], status=score['status'])
    if summary_path.exists() and events_path.exists():
        cached = read_json(summary_path)
        if cached.get('_source') == signature:
            return summary_path, events_path, cached
    commands, outputs, events = [], [], []
    instruction = final = ""
    calls = 0
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip() or not line.endswith('\n'):
                continue
            row = json.loads(line)
            kind = row['event']
            event = dict(event=kind, trace_line=number)
            if kind == 'model/request':
                calls += 1
                if not instruction:
                    inputs = row.get('request', {}).get('input', [])
                    if isinstance(inputs, list):
                        first = next((m for m in inputs if m.get('role') == 'user'), {})
                        instruction = str(first.get('content', ''))[:SUMMARY_CHARS]
                continue
            if kind == 'tool/request':
                event['command'] = row.get('command', '')[:COMMAND_CHARS]
                event['timeout_seconds'] = row.get('timeout_seconds')
                commands.append(dict(trace_line=number, command=event['command'][:SUMMARY_CHARS]))
            elif kind == 'tool/response':
                content = row.get('content', '')
                event['result'] = content[:80] + '\n' + content[-OUTPUT_CHARS:]
                outputs.append(dict(trace_line=number, result=event['result'][:80] + event['result'][-SUMMARY_CHARS:]))
            elif kind == 'model/response':
                response = row.get('response', {})
                text = response.get('output_text') or '\n'.join(
                    p.get('text', '') for item in response.get('output', [])
                    for p in item.get('content', []) if p.get('type') == 'output_text')
                if not text:
                    continue
                final = text[-SUMMARY_CHARS:]
                event['text'] = text[:OUTPUT_CHARS]
            elif kind in ('model/error', 'budget/used', 'plugin/error'):
                event.update({k: v for k, v in row.items() if k in ('error_type', 'status_code', 'model_calls', 'message')})
            else:
                continue
            events.append(event)
    summary = dict(task_id=score['task_id'], score=score['score'], status=score['status'],
                   instruction=instruction, model_calls=calls, command_count=len(commands),
                   first_commands=commands[:1], last_commands=commands[-2:], last_results=outputs[-2:],
                   final_claim=final, scope='Public trace excerpts. Verifier score overrides completion claims.',
                   _source=signature)
    temporary = events_path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
    temporary.replace(events_path)
    write_json(summary_path, summary)
    return summary_path, events_path, summary


def brief(entry):
    return {k: entry[k] for k in BRIEF_KEYS if k in entry}


def candidate_diff(request, entry):
    directory = Path(request['output_dir']).parent / entry['id']
    request_path = directory / 'proposal_request.json'
    if not request_path.exists():
        return None
    source_request = read_json(request_path)
    contract = source_request.get('local_contract')
    roots = [(Path(source_request['parent_dir']), directory/'harness')]
    if contract:
        source = next((Path(root)/contract['source_ref'] for root in source_request['library_dirs']
                       if (Path(root)/contract['source_ref']).exists()), None)
        if source is not None:
            roots.append((source, directory/contract['plugin_output_prefix']))
    patches = []
    for before, after in roots:
        for path in sorted(after.rglob('*')):
            if not path.is_file() or path.suffix not in ('.py', '.md', '.yaml') or '__pycache__' in path.parts:
                continue
            relative = path.relative_to(after)
            old = before/relative
            previous = old.read_text() if old.is_file() else ''
            current = path.read_text()
            if previous != current:
                patches.extend(difflib.unified_diff(previous.splitlines(True), current.splitlines(True),
                                                  fromfile=f'parent/{relative}', tofile=f'child/{relative}', n=2))
    text = ''.join(patches)
    if len(text) > DIFF_CHARS:
        text = text[:DIFF_CHARS] + '\n[Diff excerpted; inspect the referenced source files for the rest.]\n'
    target = directory/'evidence_change.diff'
    target.write_text(text)
    return target


def representative_ids(rows):
    selected, repos = [], set()
    failures = [r for r in rows if r['status'] in ('completed', 'solver_limit') and r['score'] < 1]
    for row in failures:
        repo = row['task_id'].split('__')[0]
        if repo not in repos:
            selected.append(row['task_id'])
            repos.add(repo)
        if len(selected) == GLOBAL_FAILURE_EXAMPLES:
            break
    for row in failures:
        if len(selected) >= GLOBAL_FAILURE_EXAMPLES:
            break
        if row['task_id'] not in selected:
            selected.append(row['task_id'])
    selected.extend(r['task_id'] for r in rows if r['status'] in ('completed', 'solver_limit') and r['score'] == 1)
    return set(selected[:min(len(failures), GLOBAL_FAILURE_EXAMPLES) + GLOBAL_CONTROL_EXAMPLES])


def prepare_compact(agent, public, summaries):
    request = agent.request
    public['history'] = {cid: {k: v for k, v in value.items() if k in ('mean_score', 'evaluation_id', 'valid_task_count')}
                         for cid, value in public.get('history', {}).items()}
    entries = request.get('local_evidence', [])
    public['local_evidence'] = [brief(entry) for entry in entries]
    top = sorted((e for e in entries if e.get('retained')), key=lambda e: (
        -(e.get('matched_gain') or 0), e['id']))[:request.get('evidence_top_k', 3)]
    public['focus_candidates'] = [entry['id'] for entry in top]
    for entry in top:
        path = candidate_diff(request, entry)
        if path:
            agent.files[f"evidence/{entry['id']}/change.diff"] = path
    public['published_plugin_evidence'] = [
        dict(plugin_ref=p['plugin_ref'], source_candidate=p['source_candidate'],
             results=[brief(s) for s in p.get('local_results', [])])
        for p in public.get('published_plugin_evidence', [])][-12:]
    if request['phase'] == 'harness_recomposition':
        rows = request['feedback']['scores']
        public.pop('feedback_scores', None)
        public['feedback_index'] = dict(columns=['task_id', 'score', 'status'],
                                        rows=[[r['task_id'], r['score'], r['status']] for r in rows])
        public['feedback_overview'] = dict(status_counts=dict(Counter(r['status'] for r in rows)),
            failures_by_repository=dict(Counter(r['task_id'].split('__')[0] for r in rows
                if r['status'] in ('completed', 'solver_limit') and r['score'] < 1)))
        public['feedback_examples'] = [{k: s[k] for k in ('task_id', 'score', 'status', 'instruction', 'model_calls', 'command_count')}
                                       for s in summaries]
        public['feedback_path_templates'] = dict(summary='feedback/{task_id}.summary.json',
                                                events='feedback/{task_id}.events.jsonl', trace='feedback/{task_id}.jsonl')
    else:
        public['feedback_index'] = [dict(task_id=s['task_id'], score=s['score'], status=s['status'],
            model_calls=s['model_calls'], command_count=s['command_count'], instruction=s['instruction'],
            summary=f"feedback/{s['task_id']}.summary.json", events=f"feedback/{s['task_id']}.events.jsonl") for s in summaries]
    public['feedback_reading'] = (
        'Use the index and paired wins/losses first. Select at most three representative failures and one regression/control '
        'to inspect in depth; do not read every summary. For integration inspect focus_candidates change.diff first. '
        'Read compact .events.jsonl views before original traces, which repeat model contexts. Original .jsonl files '
        'remain readable by their feedback or evidence path. Keep concise failure lessons; do not discard regressions. '
        'Record only evidence-supported mechanisms; a small-batch positive gain is not proof of generalization.')
    return public
