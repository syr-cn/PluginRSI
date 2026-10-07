import asyncio
import json

import pytest
from jsonschema import ValidationError

from pluginrsi.search.text_proposer import TextActionEvolver, parse_envelope
from pluginrsi.search.structured_proposer import ACTION_SCHEMA
from pluginrsi.search.json_proposer import JsonActionEvolver
from jsonschema import validate


def test_text_actions_do_not_expose_shell_or_extra_operations():
    with pytest.raises(ValidationError):
        parse_envelope('{"actions":[{"name":"shell","arguments":{"command":"echo bad"}}],"final":false}')
    with pytest.raises(ValidationError):
        parse_envelope('{"actions":[{"name":"write_file","arguments":{"path":"child/a"}}],"final":false}')


def test_structured_schema_preserves_all_file_actions():
    actions = [
        {"name": "submit_proposal", "arguments": {"hypothesis": "fixture", "changes": ["change"], "new_plugin_refs": []}},
        {"name": "read_file", "arguments": {"path": "child/workflow.py"}},
        {"name": "read_file_range", "arguments": {"path": "feedback/task.jsonl", "offset": 0, "max_chars": 10}},
        {"name": "write_file", "arguments": {"path": "child/code.py", "content": 'print("x")\n'}},
        {"name": "list_files", "arguments": {}},
    ]
    validate({"actions": actions, "final": False}, ACTION_SCHEMA)
    with pytest.raises(ValidationError):
        validate({"actions": [{"name": "read_file", "arguments": {"content": "wrong argument"}}], "final": False}, ACTION_SCHEMA)


@pytest.mark.parametrize("agent_type", [TextActionEvolver, JsonActionEvolver])
def test_text_transport_uses_no_api_tools_and_writes_the_same_scoped_files(tmp_path, monkeypatch, agent_type):
    output = tmp_path / "candidate"
    (output / "harness").mkdir(parents=True)
    request = {"output_dir": str(output), "parent_dir": str(output / "harness"), "library_dirs": [],
               "allowed_plugin_refs": [], "feedback_dir": str(tmp_path), "feedback": {"scores": []},
               "phase": "harness_recomposition", "candidate_id": "c000001", "instructions": "Write the candidate",
               "proposer_agent": {"model": "fixture", "reasoning_effort": "xhigh"}}
    agent = agent_type(request)
    monkeypatch.setattr(agent, "prepare_context", lambda: {})
    calls = []
    result = {"hypothesis": "fixture", "changes": ["write a file"], "new_plugin_refs": []}

    class Model:
        def __init__(self, config, emit):
            assert config.reasoning_effort == "xhigh"
            self.calls = 0
            self.tokens = 0

        async def complete(self, messages, tools=None, **kwargs):
            assert tools is None
            assert kwargs == ({"text_format": {"type": "json_object"}} if agent_type is JsonActionEvolver else {})
            assert "JSON" in messages[0]["content"]
            self.calls += 1
            calls.append(messages)
            return {"output_text": json.dumps({"actions": [
                {"name": "write_file", "arguments": {"path": "child/workflow.py", "content": "value = 1\n"}},
                {"name": "write_file", "arguments": {"path": "proposal_result.json", "content": json.dumps(result)}}], "final": True})}

        async def close(self):
            pass

    monkeypatch.setattr("pluginrsi.search.text_proposer.SolverAgent", Model)
    asyncio.run(agent.run())
    assert len(calls) == 1
    assert (output / "harness/workflow.py").read_text() == "value = 1\n"
    assert json.loads((output / "proposal_result.json").read_text()) == result
    assert "error:" in agent.tool("write_file", {"path": "../outside", "content": "bad"})


@pytest.mark.parametrize("repair", [True, False])
@pytest.mark.parametrize("agent_type", [TextActionEvolver, JsonActionEvolver])
def test_premature_final_is_repaired_or_rejected_within_a_bound(tmp_path, monkeypatch, repair, agent_type):
    output = tmp_path / "candidate"
    (output / "harness").mkdir(parents=True)
    request = {"output_dir": str(output), "parent_dir": str(output / "harness"), "library_dirs": [],
               "allowed_plugin_refs": [], "feedback_dir": str(tmp_path), "feedback": {"scores": []},
               "phase": "harness_recomposition", "candidate_id": "c000001", "instructions": "Write the candidate",
               "proposer_agent": {"model": "fixture", "reasoning_effort": "xhigh"}}
    agent = agent_type(request)
    monkeypatch.setattr(agent, "prepare_context", lambda: {})
    calls = []

    class Model:
        def __init__(self, *args):
            self.calls = 0
            self.tokens = 0

        async def complete(self, messages, tools=None, **kwargs):
            self.calls += 1
            calls.append(self.calls)
            if repair and self.calls == 2:
                assert "completion_error" in messages[-1]["content"]
                result = {"hypothesis": "repair", "changes": [], "new_plugin_refs": []}
                return {"output_text": json.dumps({"actions": [{"name": "write_file", "arguments": {
                    "path": "proposal_result.json", "content": json.dumps(result)}}], "final": True})}
            return {"output_text": '{"actions":[],"final":true}'}

        async def close(self):
            pass

    monkeypatch.setattr("pluginrsi.search.text_proposer.SolverAgent", Model)
    if repair:
        asyncio.run(agent.run())
        assert len(calls) == 2 and (output / "proposal_result.json").exists()
    else:
        with pytest.raises(ValueError, match="finalization corrections"):
            asyncio.run(agent.run())
        assert len(calls) == 3


@pytest.mark.parametrize('error_limit', [3, 6])
def test_configured_protocol_budget_and_live_call_reminder(tmp_path, monkeypatch, error_limit):
    output = tmp_path / 'candidate'
    (output / 'harness').mkdir(parents=True)
    request = dict(output_dir=str(output), parent_dir=str(output / 'harness'), library_dirs=[],
                   allowed_plugin_refs=[], feedback_dir=str(tmp_path), feedback={'scores': []},
                   phase='harness_recomposition', candidate_id='c1', instructions='Write files',
                   proposer_agent={'model': 'fixture', 'max_calls': error_limit},
                   proposer_max_protocol_errors=error_limit, proposer_budget_reserve_calls=2)
    agent = JsonActionEvolver(request)
    monkeypatch.setattr(agent, 'prepare_context', lambda: {})

    class Model:
        def __init__(self, *args):
            self.calls = self.tokens = 0

        async def complete(self, messages, **kwargs):
            remaining = error_limit - self.calls
            assert f'{remaining}/{error_limit} calls remaining' in messages[1]['content']
            assert ('Finish implementation' in messages[1]['content']) == (remaining <= 2)
            self.calls += 1
            if self.calls < error_limit:
                return {'output_text': '{"actions":[]}'}
            result = dict(hypothesis='fixture', changes=[], new_plugin_refs=[])
            return {'output_text': json.dumps({'actions': [
                {'name': 'submit_proposal', 'arguments': result}], 'final': True})}

        async def close(self):
            pass

    monkeypatch.setattr('pluginrsi.search.text_proposer.SolverAgent', Model)
    asyncio.run(agent.run())
    assert (output / 'proposal_result.json').exists()
    assert json.loads((output / 'proposer_usage.json').read_text())['model_calls'] == error_limit
