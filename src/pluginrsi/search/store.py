from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from ..loader import tree_contents


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text())


def snapshot(source: Path, target: Path):
    tree_contents(source)
    if target.exists():
        if tree_contents(source) != tree_contents(target):
            raise ValueError(f"refusing to overwrite immutable snapshot: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    shutil.copytree(source, temporary, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.rename(temporary, target)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "state.json"
        if self.path.exists():
            self.state = read_json(self.path)
        else:
            self.state = dict(schema_version=1, next_candidate=1, next_evaluation=0,
                              rollouts_started=0, archive={}, beam=[], phase_index=0,
                              iteration=0, phase="seed", completed_steps=[],
                              stop_reason=None, evaluation_keys={})

    def save(self):
        write_json(self.path, self.state)

    def allocate(self, kind: str) -> str:
        key = "next_" + kind
        value = self.state[key]
        self.state[key] += 1
        self.save()
        return f"{kind[0]}{value:06d}"

    def candidate(self, cid: str) -> Path:
        return self.root / "candidates" / cid
