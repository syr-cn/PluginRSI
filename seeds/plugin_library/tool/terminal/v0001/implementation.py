from pluginrsi.contracts import Tool


class Terminal(Tool):
    def schema(self) -> dict:
        return {"type": "function", "name": "terminal", "description": "Execute a shell command in the task repository.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"], "additionalProperties": False}}

    async def execute(self, arguments: dict):
        result = await self.services.environment.execute(arguments["command"], self.config["timeout_seconds"])
        result.content = result.content.encode()[-self.config["max_output_bytes"]:].decode(errors="replace")
        return result
