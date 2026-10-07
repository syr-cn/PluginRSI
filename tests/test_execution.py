import asyncio
import json
from pathlib import Path

from pluginrsi.contracts import Task, ToolResult
from pluginrsi.loader import execute, tree_contents
from pluginrsi.runtime import EventLog, Runtime

ROOT = Path(__file__).resolve().parents[1]


def test_seed_executes_tool_updates_memory_and_returns_model_output(tmp_path):
    events = EventLog(tmp_path / "trajectory.jsonl")

    class Model:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert tools[0]["name"] == "terminal"
            if self.calls == 1:
                return {"output": [{"type": "function_call", "name": "terminal", "call_id": "call1", "arguments": '{"command":"inspect"}'}],
                        "output_text": ""}
            assert any(item.get("type") == "function_call_output" for item in messages)
            assert any("observed failure" in str(item.get("content", "")) for item in messages)
            return {"output": [], "output_text": "Finished after inspecting the failure."}

    class Environment:
        async def execute(self, command, timeout):
            assert command == "inspect"
            return ToolResult("observed failure")

    library = ROOT / "seeds/plugin_library"
    before = tree_contents(library)
    runtime = Runtime(Model(), Environment(), events, ROOT / "seeds/agents/swe_bench_verified/v0001", tmp_path)
    result = asyncio.run(execute(runtime.resource_dir, [library], Task("task", "Fix observed failure"), runtime))
    assert result.output == "Finished after inspecting the failure."
    assert "observed failure" in (tmp_path / "state/experience/entries.jsonl").read_text()
    assert tree_contents(library) == before
    rows = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    inputs = {row["invocation_id"]: row for row in rows if row["event"] == "plugin/input"}
    outputs = {row["invocation_id"]: row for row in rows if row["event"] == "plugin/output"}
    assert inputs.keys() == outputs.keys()
    assert {row["producer"]["kind"] for row in inputs.values()} == {"role", "skill", "tool", "memory"}
    assert all(row["task_id"] == "task" for row in inputs.values())
    for invocation, row in outputs.items():
        assert row["producer"] == inputs[invocation]["producer"]
        assert row["producer"]["provenance"]["label"].startswith("PluginRSI-Baseline-")
    assert runtime.model.plugin_outputs
