import json
from pathlib import Path

from pluginrsi.search.api_proposer import EvolverAgent


def test_file_tools_confine_writes_and_reads(tmp_path):
    child = tmp_path / "child"
    (child / "harness").mkdir(parents=True)
    (child / "harness/workflow.py").write_text("original")
    request = {"output_dir": str(child), "parent_dir": str(child / "harness"), "library_dirs": [],
               "allowed_plugin_refs": [], "feedback_dir": str(tmp_path), "feedback": {"scores": []},
               "phase": "harness_recomposition"}
    agent = EvolverAgent(request)
    assert "error:" in agent.tool("write_file", {"path": "../outside", "content": "bad"})
    assert "error:" in agent.tool("write_file", {"path": "new_plugins/file.py", "content": "bad"})
    assert "error:" in agent.tool("read_file", {"path": "/etc/passwd"})
    assert agent.tool("write_file", {"path": "child/workflow.py", "content": "changed"}) == "written"
    assert (child / "harness/workflow.py").read_text() == "changed"
    assert not (tmp_path / "outside").exists()


def test_command_adapter_transports_json_without_shell_interpretation(tmp_path):
    import asyncio
    import sys
    from pluginrsi.schemas import ProposerConfig
    from pluginrsi.search.proposer import CommandProposer

    directory = tmp_path / "candidate with spaces"
    directory.mkdir()
    command = [sys.executable, "-c",
        "import json,sys; from pathlib import Path; r=json.loads(Path(sys.argv[1]).read_text()); "
        "Path('proposal_result.json').write_text(json.dumps({'hypothesis':r['hypothesis'],'changes':[],'new_plugin_refs':[]}))"]
    request = {"output_dir": str(directory), "hypothesis": "literal $(not_a_command) `also_literal`"}
    result = asyncio.run(CommandProposer(ProposerConfig(command=command)).propose(request))
    assert result["hypothesis"] == request["hypothesis"]
    assert (directory / "proposer.log").exists()


def test_proposer_can_read_later_feedback_without_silent_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("pluginrsi.search.api_proposer.MAX_FILE_CHARS", 6)
    child = tmp_path / "child"
    (child / "harness").mkdir(parents=True)
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text("0123456789")
    request = {"output_dir": str(child), "parent_dir": str(child / "harness"), "library_dirs": [],
               "allowed_plugin_refs": [], "feedback_dir": str(tmp_path), "phase": "plugin_mutation",
               "feedback": {"scores": [{"task_id": "task", "trajectory": "trajectory.jsonl"}]}}
    agent = EvolverAgent(request)
    first = agent.tool("read_file", {"path": "feedback/task.jsonl"})
    assert first.startswith("012345") and "of 10" in first
    last = agent.tool("read_file_range", {"path": "feedback/task.jsonl", "offset": 6, "max_chars": 4})
    assert last.startswith("6789") and "6:10 of 10" in last
    assert "error:" in agent.tool("read_file_range", {"path": "/etc/passwd", "offset": 0, "max_chars": 4})


def test_metadata_submission_checks_types_before_writing(tmp_path):
    child = tmp_path / "candidate"
    (child / "harness").mkdir(parents=True)
    request = {"output_dir": str(child), "parent_dir": str(child / "harness"), "library_dirs": [],
               "allowed_plugin_refs": [], "feedback_dir": str(tmp_path), "feedback": {"scores": []},
               "phase": "harness_recomposition"}
    agent = EvolverAgent(request)
    bad = {"hypothesis": "change", "changes": [{"asset": "workflow"}], "new_plugin_refs": []}
    assert "error:" in agent.tool("submit_proposal", bad)
    assert not (child / "proposal_result.json").exists()
    good = {"hypothesis": "change", "changes": ["Update workflow"], "new_plugin_refs": []}
    assert agent.tool("submit_proposal", good) == "written"
    assert json.loads((child / "proposal_result.json").read_text()) == good
