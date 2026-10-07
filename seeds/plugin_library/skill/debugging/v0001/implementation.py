from pluginrsi.contracts import Skill, SkillContent


class Debugging(Skill):
    def load(self, context: dict) -> SkillContent:
        return SkillContent((self.services.resource_dir / "SKILL.md").read_text())
