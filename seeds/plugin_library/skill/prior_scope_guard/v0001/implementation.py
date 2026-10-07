import yaml

from pluginrsi.contracts import Skill, SkillContent


class PriorSkill(Skill):
    def load(self, context: dict) -> SkillContent:
        root = self.services.resource_dir
        resources = {"asset.yaml": (root / "asset.yaml").read_text()}
        directory = root / "resources"
        if directory.exists():
            resources.update({str(path.relative_to(root)): path.read_text()
                              for path in sorted(directory.rglob("*")) if path.is_file()})
        return SkillContent((root / "SKILL.md").read_text(), resources)
