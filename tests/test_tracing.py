import asyncio
import json
from pathlib import Path

from pluginrsi.contracts import Memory, PluginServices
from pluginrsi.runtime import EventLog, Runtime
from pluginrsi.schemas import PluginManifest
from pluginrsi.tracing import ORIGIN, instrument, origin


def test_plugin_internal_model_events_keep_identity_and_reset_on_return(tmp_path):
    emit = EventLog(tmp_path / "trace.jsonl")

    class Model:
        async def complete(self, messages):
            emit("model/request", call=1, request=messages)
            emit("model/response", call=1, response={"output": "summary"})
            return "summary"

    class Summarizer(Memory):
        async def update(self, experience):
            await self.services.model.complete([experience])

        async def retrieve(self, query):
            return []

    runtime = Runtime(Model(), None, emit, tmp_path, tmp_path)
    manifest = PluginManifest(kind="memory", name="summary", version="v0001", entrypoint="implementation:Summarizer",
        description="fixture", provenance={"label": "Paper-Memory", "sources": [{"id": "paper"}]})
    plugin = Summarizer({}, PluginServices(runtime.model, None, emit, tmp_path, tmp_path))
    instrument(plugin, manifest, "bank", runtime)
    with origin({"producer": {"kind": "workflow"}, "task_id": "task"}):
        asyncio.run(plugin.update({"content": "experience"}))
        assert ORIGIN.get()["producer"]["kind"] == "workflow"
        emit("workflow/continued")
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    model_rows = [row for row in rows if row["event"].startswith("model/")]
    assert len(model_rows) == 2
    assert all(row["producer"]["plugin_ref"] == "memory/summary/v0001" for row in model_rows)
    assert all(row["task_id"] == "task" for row in model_rows)
    assert model_rows[0]["invocation_id"] == model_rows[1]["invocation_id"]
    assert rows[-1]["producer"]["kind"] == "workflow"
    assert ORIGIN.get() == {}
