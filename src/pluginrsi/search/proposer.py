from __future__ import annotations

import asyncio
import ast
import os
import signal
import sys
import yaml
from pathlib import Path

from ..loader import library_refs, resolve_plugin, tree_contents, validate
from ..runtime import InfraError
from ..schemas import HarnessManifest, ProposalResult, read_yaml
from .store import read_json, write_json, snapshot


def validate_no_explicit_hashes(root):
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            prohibited = (isinstance(node, ast.Import) and any(a.name == "hashlib" for a in node.names)
                          or isinstance(node, ast.ImportFrom) and node.module == "hashlib"
                          or isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hash")
            if prohibited:
                raise ValueError("explicit content hashing is prohibited; use direct string equality for deduplication")


def prepare_local_plugin(request):
    directory = Path(request["output_dir"])
    contract = request["local_contract"]
    source_ref = contract["source_ref"]
    ref = source_ref.rsplit("/", 1)[0] + "/" + request["version"]
    source, _ = resolve_plugin(source_ref, list(map(Path, request["library_dirs"])))
    package = directory / "new_plugins" / ref
    if not package.exists():
        snapshot(source, package)
    manifest = read_yaml(package / "plugin.yaml")
    manifest["version"] = request["version"]
    manifest["provenance"]["parent_refs"] = [source_ref]
    (package / "plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    harness_path = directory / "harness/harness.yaml"
    harness = read_yaml(harness_path)
    harness["plugins"][contract["alias"]]["ref"] = ref
    harness_path.write_text(yaml.safe_dump(harness, sort_keys=False))
    contract["plugin_output_prefix"] = f"new_plugins/{ref}"
    contract["new_plugin_ref"] = ref


async def run_command(command: list[str], cwd: Path, log_path: Path, timeout: float, env=None):
    with log_path.open("ab") as log:
        process = await asyncio.create_subprocess_exec(*command, cwd=cwd, stdout=log, stderr=log,
                                                        start_new_session=True, env=env)
        try:
            return await asyncio.wait_for(process.wait(), timeout)
        finally:
            def signal_group(sig):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            signal_group(signal.SIGTERM)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except (TimeoutError, asyncio.TimeoutError):
                    signal_group(signal.SIGKILL)
                    await process.wait()
            signal_group(signal.SIGKILL)



class CommandProposer:
    def __init__(self, config):
        self.config = config

    async def propose(self, request: dict) -> dict:
        directory = Path(request["output_dir"])
        path = directory / "proposal_request.json"
        write_json(path, request)
        command = list(self.config.command)
        if command[0] == "python3":
            command[0] = sys.executable
        failure = directory / "proposal_failure.json"
        failure.unlink(missing_ok=True)
        code = await run_command(command + [str(path)], directory,
                                 directory / "proposer.log", self.config.timeout_seconds)
        if code < 0:
            raise InfraError(f"proposer terminated by signal {-code}; see proposer.log")
        if code:
            if failure.exists() and read_json(failure).get("kind") == "infra_error":
                raise InfraError(read_json(failure)["detail"])
            raise ValueError(f"proposer exited with status {code}; see proposer.log")
        return read_json(directory / "proposal_result.json")


def validate_proposal(directory: Path, cid: str, phase: str, libraries: list[Path], allowed_refs: list[str], result: dict, load_code=True):
    ProposalResult.model_validate(result)
    validate_no_explicit_hashes(directory / "harness")
    validate_no_explicit_hashes(directory / "new_plugins")
    proposed = library_refs([directory / "new_plugins"])
    if phase == "plugin_mutation":
        if len(proposed) != 1 or proposed[0].split("/")[-1] != f"v_{cid}":
            raise ValueError("plugin mutation must create exactly one plugin with controller-assigned version")
        if proposed[0] in allowed_refs:
            raise ValueError("new plugin ref is already published")
        if any(not name.startswith(proposed[0] + "/") for name in tree_contents(directory / "new_plugins")):
            raise ValueError("new_plugins may contain only the single proposed package")
    elif proposed or any((directory / "new_plugins").rglob("*")):
        raise ValueError("recomposition cannot create plugin files")
    if sorted(result.get("new_plugin_refs", [])) != proposed:
        raise ValueError("declared plugin refs do not match generated packages")
    manifest = HarnessManifest.model_validate(read_yaml(directory / "harness" / "harness.yaml"))
    used = {item.ref for item in manifest.plugins.values()}
    if not set(proposed) <= used or not used <= set(allowed_refs) | set(proposed):
        raise ValueError("new plugin must be referenced; other refs must belong to frozen library")
    for ref in proposed:
        _, plugin = resolve_plugin(ref, [directory / "new_plugins"])
        provenance = plugin.provenance
        if (not isinstance(provenance.get("label"), str) or not provenance["label"].strip()
                or not isinstance(provenance.get("sources"), list)
                or not isinstance(provenance.get("upstream_assets"), list)
                or not isinstance(provenance.get("adaptation"), str)):
            raise ValueError("new plugin requires a provenance label, sources, upstream_assets and adaptation")
        parents = plugin.provenance.get("parent_refs", [])
        if not isinstance(parents, list) or not all(isinstance(parent, str) and parent in allowed_refs for parent in parents):
            raise ValueError("provenance parent_refs must refer to the frozen published library")
    validate(directory / "harness", [directory / "new_plugins", *libraries], load_code=load_code)
    return proposed
