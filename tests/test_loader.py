import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from pluginrsi.contracts import PluginServices
from pluginrsi.loader import apply_defaults, load_class, resolve_plugin, validate
from pluginrsi.search.proposer import validate_proposal

ROOT = Path(__file__).resolve().parents[1]


def test_distinct_versions_relative_imports_and_alias_state(tmp_path):
    library = tmp_path / "library"
    shutil.copytree(ROOT / "seeds/plugin_library", library)
    original = library / "memory/experience_bank/v0001"
    alternate = library / "memory/experience_bank/v0002"
    shutil.copytree(original, alternate)
    manifest = yaml.safe_load((alternate / "plugin.yaml").read_text())
    manifest["version"] = "v0002"
    (alternate / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    (alternate / "helper.py").write_text("LABEL = 'alternate'\n")
    (alternate / "implementation.py").write_text(
        "from .helper import LABEL\nfrom pluginrsi.contracts import Memory\n"
        "class ExperienceBank(Memory):\n"
        "    async def update(self, experience): pass\n"
        "    async def retrieve(self, query): return [LABEL]\n")
    first = load_class(original, "implementation:ExperienceBank")
    second = load_class(alternate, "implementation:ExperienceBank")
    objects = []
    for alias in ("one", "two", "next_evaluation"):
        state = tmp_path / alias
        state.mkdir()
        services = PluginServices(None, None, lambda *a, **kw: None, original, state)
        objects.append(first({"top_k": 5}, services))
    asyncio.run(objects[0].update({"content": "failure fixed"}))
    assert len(asyncio.run(objects[0].retrieve({}))) == 1
    assert asyncio.run(objects[1].retrieve({})) == []
    assert asyncio.run(objects[2].retrieve({})) == []
    assert asyncio.run(second({}, services).retrieve({})) == ["alternate"]
    assert not (original / "entries.jsonl").exists()


def test_defaults_and_immutable_duplicate(tmp_path):
    schema = {"type": "object", "properties": {"nested": {"type": "object", "default": {},
        "properties": {"n": {"type": "integer", "default": 3}}}}}
    value = {}
    assert apply_defaults(schema, value) == {"nested": {"n": 3}}
    assert value == {}
    shutil.copytree(ROOT / "seeds/plugin_library", tmp_path / "copy")
    ref = "role/coder/v0001"
    resolve_plugin(ref, [ROOT / "seeds/plugin_library", tmp_path / "copy"])
    (tmp_path / "copy" / ref / "system_prompt.md").write_text("changed")
    with pytest.raises(ValueError, match="conflicting"):
        resolve_plugin(ref, [ROOT / "seeds/plugin_library", tmp_path / "copy"])


def test_seed_and_invalid_recomposition(tmp_path):
    validate(ROOT / "seeds/agents/swe_bench_verified/v0001", [ROOT / "seeds/plugin_library"])
    child = tmp_path / "child"
    shutil.copytree(ROOT / "seeds/agents/swe_bench_verified/v0001", child / "harness")
    new = child / "new_plugins"
    new.mkdir()
    (new / "unexpected.py").write_text("x=1")
    with pytest.raises(ValueError, match="recomposition"):
        validate_proposal(child, "c000001", "harness_recomposition", [ROOT / "seeds/plugin_library"], [], {"hypothesis": "", "changes": [], "new_plugin_refs": []})


def test_reject_unknown_schema_and_bad_config(tmp_path):
    harness = tmp_path / "harness"
    shutil.copytree(ROOT / "seeds/agents/swe_bench_verified/v0001", harness)
    manifest = yaml.safe_load((harness / "harness.yaml").read_text())
    manifest["schema_version"] = 2
    (harness / "harness.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError):
        validate(harness, [ROOT / "seeds/plugin_library"])
    from jsonschema import ValidationError
    with pytest.raises(ValidationError):
        apply_defaults({"type": "object", "additionalProperties": False}, {"unexpected": 1})


def test_evolved_plugin_cannot_drop_source_metadata(tmp_path):
    child = tmp_path / "child"
    shutil.copytree(ROOT / "seeds/agents/swe_bench_verified/v0001", child / "harness")
    ref = "role/coder/v_c000001"
    package = child / "new_plugins" / ref
    shutil.copytree(ROOT / "seeds/plugin_library/role/coder/v0001", package)
    plugin = yaml.safe_load((package / "plugin.yaml").read_text())
    plugin["version"] = "v_c000001"
    plugin.pop("provenance")
    (package / "plugin.yaml").write_text(yaml.safe_dump(plugin))
    harness_path = child / "harness/harness.yaml"
    harness = yaml.safe_load(harness_path.read_text())
    harness["plugins"]["coder"]["ref"] = ref
    harness_path.write_text(yaml.safe_dump(harness))
    refs = [item["ref"] for item in harness["plugins"].values() if item["ref"] != ref]
    result = {"hypothesis": "new role", "changes": ["replace coder"], "new_plugin_refs": [ref]}
    with pytest.raises(ValueError, match="provenance"):
        validate_proposal(child, "c000001", "plugin_mutation", [ROOT / "seeds/plugin_library"], refs, result)
