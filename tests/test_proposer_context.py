import asyncio
import json

import pytest

from pluginrsi.runtime import InfraError
from pluginrsi.search.text_proposer import bound_context, context_size, TextActionEvolver


def exchange(value):
    return [dict(role='assistant', content='{"actions":[],"final":false}'),
            dict(role='user', content=json.dumps({'action_results': [
                {'name': 'read_file_range', 'source': {'path': 'feedback/t.jsonl', 'offset': 60000}, 'result': value},
                {'name': 'write_file', 'result': 'written'},
                {'name': 'submit_proposal', 'result': 'error: fix the manifest'}]}))]


def test_small_context_is_unchanged():
    messages = [dict(role='system', content='rules'), dict(role='user', content='context')]
    bounded, stats = bound_context(messages, 10000)
    assert bounded is messages and stats is None


def test_large_reads_are_excerpted_without_losing_rules_sources_or_write_receipts():
    prefix = [dict(role='system', content='immutable rules'), dict(role='user', content='immutable context')]
    messages = prefix + exchange('开始' + 'x' * 100000 + '结束')
    original = json.dumps(messages)
    bounded, stats = bound_context(messages, 10000)
    assert bounded[:2] == prefix and context_size(bounded) <= 10000
    results = json.loads(bounded[-1]['content'])['action_results']
    assert results[0]['result'].startswith('开始') and results[0]['result'].endswith('结束')
    assert 'excerpted' in results[0]['result'] and results[0]['source']['offset'] == 60000
    assert results[1]['result'] == 'written' and results[2]['result'].startswith('error:')
    assert stats['shortened_reads'] == 1 and json.dumps(messages) == original


def test_old_exchanges_can_be_removed_with_notice_but_latest_pair_is_kept():
    prefix = [dict(role='system', content='rules'), dict(role='user', content='context')]
    history = [dict(role='assistant', content='old code ' * 1000), dict(role='user', content='written')] * 5
    latest = [dict(role='assistant', content='latest action'), dict(role='user', content='latest result')]
    bounded, stats = bound_context(prefix + history + latest, 2000)
    assert bounded[:2] == prefix and bounded[-2:] == latest
    assert any('Earlier interactions were omitted' in m['content'] for m in bounded)
    assert stats['dropped_messages'] == 10 and context_size(bounded) <= 2000


def test_oversized_protected_context_fails_without_truncating_instructions():
    messages = [dict(role='system', content='x' * 20000), dict(role='user', content='context')]
    with pytest.raises(InfraError, match='Protected proposer context'):
        bound_context(messages, 10000)
    assert len(messages[0]['content']) == 20000


def test_real_text_agent_bounds_read_results_before_next_model_call(tmp_path, monkeypatch):
    output = tmp_path/'candidate';(output/'harness').mkdir(parents=True)
    evidence = tmp_path/'evidence.txt';evidence.write_text('evidence ' * 10000)
    request = dict(output_dir=str(output), parent_dir=str(output/'harness'), library_dirs=[],
        allowed_plugin_refs=[], feedback_dir=str(tmp_path), feedback={'scores': []},
        phase='harness_recomposition', candidate_id='c1', instructions='Write files',
        proposer_agent={'model': 'fixture'}, proposer_context_max_bytes=16384,
        extra_readable_files={'evidence.txt': str(evidence)})
    agent = TextActionEvolver(request)
    monkeypatch.setattr(agent, 'prepare_context', lambda: {})

    class Model:
        def __init__(self, *args):
            self.calls = self.tokens = 0
        async def complete(self, messages, *args, **kwargs):
            assert context_size(messages) <= 16384
            self.calls += 1
            if self.calls == 1:
                actions = [{'name': 'read_file', 'arguments': {'path': 'evidence.txt'}}]
                final = False
            else:
                assert 'excerpted' in messages[-1]['content']
                actions = [{'name': 'submit_proposal', 'arguments': {'hypothesis': 'fixture', 'changes': [], 'new_plugin_refs': []}}]
                final = True
            return {'output_text': json.dumps({'actions': actions, 'final': final})}
        async def close(self):
            pass

    monkeypatch.setattr('pluginrsi.search.text_proposer.SolverAgent', Model)
    asyncio.run(agent.run())
    events = [json.loads(l) for l in (output/'proposer_trace.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'proposer/context_compacted' for e in events)
    raw = next(e for e in events if e['event'] == 'proposer/file_action' and e['action']['name'] == 'read_file')
    assert len(raw['result']) > 50000
    assert (output/'proposal_result.json').exists()
