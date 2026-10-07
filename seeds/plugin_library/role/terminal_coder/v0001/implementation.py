from pluginrsi.contracts import Role


class Coder(Role):
    def render(self, context: dict) -> str:
        prompt = (self.services.resource_dir / "system_prompt.md").read_text()
        return prompt + "\n" + context.get("guidance", "")
