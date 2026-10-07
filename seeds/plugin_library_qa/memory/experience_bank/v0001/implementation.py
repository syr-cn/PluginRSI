import json
import re

from pluginrsi.contracts import Memory, MemoryItem


class ExperienceBank(Memory):
    def __init__(self, config, services):
        super().__init__(config, services)
        self.entries = []
        self.path = services.state_dir / "entries.jsonl"

    async def update(self, experience: dict) -> None:
        item = {"id": str(len(self.entries)), "content": str(experience.get("content", "")),
                "metadata": experience.get("metadata", {})}
        self.entries.append(item)
        with self.path.open("a") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    async def retrieve(self, query: dict) -> list[MemoryItem]:
        words = set(re.findall(r"\w+", str(query).lower()))
        ranked = sorted(enumerate(self.entries), key=lambda pair: (
            len(words & set(re.findall(r"\w+", pair[1]["content"].lower()))), pair[0]), reverse=True)
        return [MemoryItem(**item) for _, item in ranked[:self.config["top_k"]]]
