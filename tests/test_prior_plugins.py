import asyncio
import json
from pathlib import Path
import subprocess

import pytest
import yaml
from jsonschema import ValidationError

from pluginrsi.contracts import Memory, PluginServices, Role, Skill, Tool, ToolResult
from pluginrsi.loader import apply_defaults, library_refs, load_class, resolve_plugin

LIBRARY = Path(__file__).resolve().parents[1] / "seeds/plugin_library"
CATALOG = yaml.safe_load((LIBRARY / "prior_catalog.yaml").read_text())
RECORDS = CATALOG["plugins"]


class LocalEnvironment:
    def __init__(self, root):
        self.root = root

    async def execute(self, command, timeout_seconds):
        process = await asyncio.create_subprocess_shell(command, cwd=self.root,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        return ToolResult(f"exit={process.returncode}\nstdout={stdout.decode()}\nstderr={stderr.decode()}",
                          process.returncode != 0)


def make_plugin(ref, state, environment=None, config=None):
    root, manifest = resolve_plugin(ref, [LIBRARY])
    cls = load_class(root, manifest.entrypoint)
    state.mkdir(parents=True, exist_ok=True)
    return cls(apply_defaults(manifest.config_schema, config or {}),
               PluginServices(None, environment, lambda *args, **kwargs: None, root, state))


def tool(name, state, environment):
    return make_plugin(f"tool/prior_{name}/v0001", state / name, environment)


def execute(plugin, arguments):
    result = asyncio.run(plugin.execute(arguments))
    if result.content.startswith("exit="):
        content = result.content.partition("\nstdout=")[2].rpartition("\nstderr=")[0]
        return result, json.loads(content)
    return result, json.loads(result.content)


def test_inventory_has_complete_supported_coverage():
    assert len(RECORDS) == 71
    assert {kind: sum(row["representation"] == kind for row in RECORDS)
            for kind in ("role", "skill", "tool", "memory")} == {"role": 19, "skill": 30, "tool": 15, "memory": 7}
    refs = library_refs([LIBRARY])
    assert all(row["plugin_ref"] in refs for row in RECORDS)
    guidance = LIBRARY / "skill/prior_guidance/v0001"
    actual = {str(path.relative_to(guidance / "resources/prior"))
              for path in (guidance / "resources/prior").rglob("*") if path.is_file()}
    assert actual == {row["upstream_path"] for row in CATALOG["files"]}
    assert len(actual) == 75
    assert all(row["plugin_refs"] for row in CATALOG["files"])
    workflows = [row for row in CATALOG["files"] if row["upstream_path"].startswith("scaffold/workflows/")]
    assert len(workflows) == 7
    assert all(row["representation"] == "reference guidance only" for row in workflows)


@pytest.mark.parametrize("record", RECORDS, ids=lambda row: row["asset_id"])
def test_every_adaptation_loads_and_preserves_contract(record, tmp_path):
    plugin = make_plugin(record["plugin_ref"], tmp_path)
    root, manifest = resolve_plugin(record["plugin_ref"], [LIBRARY])
    assert isinstance(plugin, {"role": Role, "skill": Skill, "tool": Tool, "memory": Memory}[manifest.kind])
    assert manifest.provenance["label"].startswith(record["asset_id"] + " ")
    assert "not verbatim paper code" in manifest.provenance["adaptation"]
    assert {source["id"] for source in manifest.provenance["sources"]} == set(record["source_ids"])
    assert manifest.provenance["upstream_assets"][0]["id"] == record["asset_id"]
    spec = yaml.safe_load((root / "asset.yaml").read_text())
    if isinstance(plugin, Role):
        rendered = plugin.render({"issue": "literal {issue} evidence"})
        assert spec["system_prompt"] in rendered
        assert all(rule in rendered for rule in spec.get("guardrails", []))
        assert "literal {issue} evidence" in rendered
    if isinstance(plugin, Skill):
        content = plugin.load({})
        assert content.instructions == (root / "SKILL.md").read_text()
        assert "asset.yaml" in content.resources
    if isinstance(plugin, Tool):
        assert plugin.schema()["name"] == spec["name"]
        assert all(guard in plugin.schema()["description"] for guard in spec["guards"])


@pytest.mark.parametrize("record", [row for row in RECORDS if row["representation"] == "memory"], ids=lambda row: row["asset_id"])
def test_memory_validates_retains_and_reloads_evidence(record, tmp_path):
    plugin = make_plugin(record["plugin_ref"], tmp_path)
    with pytest.raises(ValueError, match="missing required fields"):
        asyncio.run(plugin.update({}))
    data = {field: "evidence" for field in plugin.spec["required_fields"]}
    if "events" in data:
        data["events"] = [{field: "evidence" for field in plugin.spec["event_fields"]}]
    asyncio.run(plugin.update(data))
    reloaded = make_plugin(record["plugin_ref"], tmp_path)
    results = asyncio.run(reloaded.retrieve({"query": "evidence"}))
    assert len(results) == 1
    assert json.loads(results[0].content) == data
    assert results[0].metadata["evidence_only"] is True
    assert results[0].metadata["promotion"] == "unreviewed"


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def answer():\n    return 41\n")
    (root / "test_module.py").write_text("from module import answer\n\ndef test_answer():\n    assert answer() == 42\n")
    (root / "AGENTS.md").write_text("Run focused checks using python3 -m pytest test_module.py.\n")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored/private.txt").write_text("answer hidden\n")
    for command in (["git", "init", "-q"], ["git", "add", "."],
                    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"]):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root, LocalEnvironment(root)


@pytest.mark.parametrize("name,arguments,key", [
    ("shell_exec", {"command": "printf hello", "cwd": ".", "timeout_seconds": 5}, "stdout"),
    ("list_tree", {"root": ".", "max_depth": 2, "max_entries": 10}, "entries"),
    ("read_file", {"path": "module.py", "max_bytes": 1024}, "content"),
    ("read_range", {"path": "module.py", "start_line": 1, "end_line": 2}, "actual_end"),
    ("text_search", {"query": "answer", "paths": ["."], "max_results": 10}, "matches"),
    ("symbol_search", {"symbol_or_query": "answer", "kind": "both", "max_results": 10}, "symbols"),
    ("repo_map", {"repository_root": ".", "token_budget": 100}, "ranked_symbols"),
    ("git_status", {"repository_root": "."}, "untracked"),
    ("git_diff", {"max_bytes": 1}, "changed_files"),
    ("run_test", {"command": "printf checked", "cwd": ".", "timeout_seconds": 5, "test_scope": "fixture"}, "status"),
    ("run_lint", {"command": "printf checked", "cwd": ".", "timeout_seconds": 5}, "diagnostics"),
    ("inspect_failure", {"command": "test", "stdout": "", "stderr": "ValueError: broken", "exit_code": 1}, "failure_class"),
    ("test_discovery", {"symbols": ["answer"], "repository_root": ".", "max_results": 10}, "evidence"),
])
def test_tools_execute_through_supplied_environment(name, arguments, key, tmp_path, repository):
    root, environment = repository
    result, data = execute(tool(name, tmp_path / "state", environment), arguments)
    assert not result.is_error, result.content
    assert key in data
    if name in {"list_tree", "text_search"}:
        assert "ignored/private.txt" not in json.dumps(data)
    if name == "symbol_search":
        assert data["approximate"] is True
    if name == "repo_map":
        assert data["token_estimate"] <= arguments["token_budget"]
    if name == "inspect_failure":
        assert data[key] == "ValueError"
    if name == "test_discovery":
        assert data["commands"] == []
        assert any(entry["path"] == "AGENTS.md" for entry in data["evidence"])


def test_patch_defaults_to_validation_then_applies_and_preserves_diff(tmp_path, repository):
    root, environment = repository
    plugin = tool("apply_patch", tmp_path / "state", environment)
    patch = [{"path": "module.py", "old": "return 41", "new": "return 42"}]
    result, data = execute(plugin, {"patch": patch})
    assert not result.is_error and data["valid"] and not data["applied"]
    assert "return 41" in (root / "module.py").read_text()
    result, data = execute(plugin, {"patch": patch, "dry_run": False})
    assert not result.is_error and data["applied"]
    assert "return 42" in (root / "module.py").read_text()
    result, data = execute(tool("git_diff", tmp_path / "state", environment), {"max_bytes": 1})
    assert data["truncated"] and "module.py" in data["changed_files"]


def test_tools_reject_traversal_symlinks_binary_and_invalid_bounds(tmp_path, repository):
    root, environment = repository
    (tmp_path / "outside.txt").write_text("outside")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    (root / "binary.bin").write_bytes(b"\x00data")
    plugin = tool("read_file", tmp_path / "state", environment)
    for path in ("../outside.txt", "escape.txt", "binary.bin"):
        result, data = execute(plugin, {"path": path, "max_bytes": 100})
        assert result.is_error and "error" in data
    with pytest.raises(ValidationError):
        asyncio.run(plugin.execute({"path": "module.py", "max_bytes": 0}))
    result, data = execute(tool("read_range", tmp_path / "state", environment),
                           {"path": "module.py", "start_line": 4, "end_line": 1})
    assert result.is_error
    result, data = execute(tool("apply_patch", tmp_path / "state", environment),
                           {"patch": [{"path": "../outside.txt", "old": "outside", "new": "changed"}], "dry_run": False})
    assert result.is_error
    assert (tmp_path / "outside.txt").read_text() == "outside"


def test_run_test_preserves_failure_and_timeout(tmp_path, repository):
    _, environment = repository
    plugin = tool("run_test", tmp_path / "state", environment)
    _, data = execute(plugin, {"command": "exit 3", "cwd": ".", "timeout_seconds": 5, "test_scope": "fixture"})
    assert data["status"] == "fail" and data["exit_code"] == 3
    _, data = execute(plugin, {"command": "sleep 10", "cwd": ".", "timeout_seconds": 1, "test_scope": "fixture"})
    assert data["status"] == "timeout"


def test_submission_requires_gate_and_writes_prediction(tmp_path):
    plugin = tool("submit_patch", tmp_path, None)
    args = {"task_id": "fixture", "patch": "diff", "evidence_summary": {}, "workflow_version": "v1"}
    result = asyncio.run(plugin.execute(args))
    assert result.is_error and not (tmp_path / "submit_patch/prediction.json").exists()
    args["evidence_summary"]["completion_decision"] = "submit"
    result, data = execute(plugin, args)
    assert not result.is_error
    assert json.loads(Path(data["artifact_path"]).read_text())["model_patch"] == "diff"
    assert result.artifacts == [data["artifact_path"]]
