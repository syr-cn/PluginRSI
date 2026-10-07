from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from .contracts import Memory, PluginServices, Role, Skill, Tool
from .schemas import HarnessManifest, PluginManifest, read_yaml
from .tracing import instrument, origin

BASES = {"role": Role, "tool": Tool, "skill": Skill, "memory": Memory}


@contextmanager
def no_bytecode_writes():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def tree_contents(root: Path) -> dict[str, bytes]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"package symlinks are not supported: {path}")
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def apply_defaults(schema: dict, config: dict) -> dict:
    def properties(validator, properties, instance, full_schema):
        if isinstance(instance, dict):
            for key, value in properties.items():
                if "default" in value:
                    instance.setdefault(key, deepcopy(value["default"]))
        yield from Draft202012Validator.VALIDATORS["properties"](validator, properties, instance, full_schema)

    Draft202012Validator.check_schema(schema)
    result = deepcopy(config)
    validators.extend(Draft202012Validator, {"properties": properties})(schema).validate(result)
    return result


def load_class(root: Path, entrypoint: str):
    tree_contents(root)
    module, name = entrypoint.split(":")
    namespace = f"_pluginrsi_{uuid.uuid4().hex}"
    init = root / "__init__.py"
    if not init.is_file():
        raise ValueError(f"package requires {init}")
    spec = importlib.util.spec_from_file_location(namespace, init, submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    with no_bytecode_writes():
        spec.loader.exec_module(package)
        cls = getattr(importlib.import_module(f"{namespace}.{module}"), name)
    if not inspect.isclass(cls):
        raise ValueError(f"entrypoint is not a class: {entrypoint}")
    return cls


def resolve_plugin(ref: str, libraries: list[Path]) -> tuple[Path, PluginManifest]:
    matches = [root / ref for root in libraries if (root / ref / "plugin.yaml").is_file()]
    if not matches:
        raise ValueError(f"unpublished or missing plugin: {ref}")
    source = tree_contents(matches[0])
    if any(tree_contents(path) != source for path in matches[1:]):
        raise ValueError(f"conflicting implementations for immutable ref: {ref}")
    manifest = PluginManifest.model_validate(read_yaml(matches[0] / "plugin.yaml"))
    if manifest.ref != ref:
        raise ValueError(f"plugin path disagrees with manifest: {ref}")
    return matches[0], manifest


def library_refs(libraries: list[Path]) -> list[str]:
    refs = sorted({str(p.parent.relative_to(root)) for root in libraries for p in root.glob("*/*/*/plugin.yaml")})
    for ref in refs:
        resolve_plugin(ref, libraries)
    return refs


def validate(harness: Path, libraries: list[Path], load_code=True):
    manifest = HarnessManifest.model_validate(read_yaml(harness / "harness.yaml"))
    workflow = None
    if load_code:
        workflow = load_class(harness, manifest.entrypoint)
        if not inspect.iscoroutinefunction(getattr(workflow, "run", None)):
            raise ValueError("workflow must define async run(task, runtime, plugins)")
    resolved = {}
    for alias, instance in manifest.plugins.items():
        root, plugin = resolve_plugin(instance.ref, libraries)
        cls = None
        if load_code:
            cls = load_class(root, plugin.entrypoint)
            if not issubclass(cls, BASES[plugin.kind]) or inspect.isabstract(cls):
                raise ValueError(f"{instance.ref} must implement {plugin.kind} interface")
        resolved[alias] = (root, cls, apply_defaults(plugin.config_schema, instance.config))
    return workflow, resolved


async def execute(harness: Path, libraries: list[Path], task, runtime):
    scope = {"producer": {"kind": "workflow"}, "candidate_id": harness.parent.name, "task_id": task.id}
    with no_bytecode_writes(), origin(scope):
        return await _execute(harness, libraries, task, runtime)


async def _execute(harness: Path, libraries: list[Path], task, runtime):
    workflow, resolved = validate(harness, libraries)
    plugins = {}
    for alias, (root, cls, config) in resolved.items():
        state = runtime.work_dir / "state" / alias
        state.mkdir(parents=True, exist_ok=False)
        def emit(event, _alias=alias, **data):
            runtime.emit(event, plugin_alias=_alias, **data)
        manifest = PluginManifest.model_validate(read_yaml(root / "plugin.yaml"))
        plugin = cls(config, PluginServices(runtime.model, runtime.environment, emit, root, state))
        plugins[alias] = instrument(plugin, manifest, alias, runtime)
        runtime.emit("plugin/loaded", plugin_alias=alias, plugin_ref=manifest.ref, provenance=manifest.provenance, config=config)
    return await workflow().run(task, runtime, plugins)
