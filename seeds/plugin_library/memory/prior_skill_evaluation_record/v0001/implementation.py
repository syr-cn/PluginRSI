import json
import re

import yaml

from pluginrsi.contracts import Memory, MemoryItem


class PriorMemory(Memory):
    def __init__(self, config, services):
        super().__init__(config, services)
        self.spec = yaml.safe_load((services.resource_dir / "asset.yaml").read_text())
        self.path = services.state_dir / "records.jsonl"
        self.entries = [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []

    async def update(self, experience: dict) -> None:
        missing = set(self.spec["required_fields"]) - experience.keys()
        if missing:
            raise ValueError(f"{self.spec['id']} missing required fields: {sorted(missing)}")
        if self.spec.get("event_fields"):
            if not isinstance(experience["events"], list):
                raise ValueError("events must be a list")
            for event in experience["events"]:
                if not isinstance(event, dict) or set(self.spec["event_fields"]) - event.keys():
                    raise ValueError("trajectory event does not satisfy event_fields")
        content = json.dumps(experience, ensure_ascii=False, allow_nan=False)
        item = {"id": f"{self.spec['id']}:{len(self.entries)}", "content": content,
                "metadata": {"record_type": self.spec["id"], "evidence_only": True,
                             "retention": self.spec["retention"], "promotion": "unreviewed"}}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.entries.append(item)

    async def retrieve(self, query: dict) -> list[MemoryItem]:
        words = set(re.findall(r"\w+", json.dumps(query, ensure_ascii=False).lower()))
        ranked = sorted(enumerate(self.entries), key=lambda pair: (
            len(words & set(re.findall(r"\w+", pair[1]["content"].lower()))), pair[0]), reverse=True)
        return [MemoryItem(**item) for _, item in ranked[:self.config.get("top_k", 5)]]
