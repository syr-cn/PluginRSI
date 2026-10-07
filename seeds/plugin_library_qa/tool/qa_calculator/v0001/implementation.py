from pluginrsi.contracts import Tool


class Calculator(Tool):
    def schema(self) -> dict:
        return {"type": "function", "name": "calculator", "description": "Evaluate numeric arithmetic with +, -, *, /, //, %, **; sqrt, log, log10, exp, sin, cos, tan, asin, acos, atan, floor, ceil, abs, round, min, max; and constants pi, e, tau. Uses real floating-point arithmetic; no symbolic variables, files or Python statements.",
                "parameters": {"type": "object", "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 2048}},
                               "required": ["expression"], "additionalProperties": False}}

    async def execute(self, arguments: dict):
        result = await self.services.environment.calculate(arguments["expression"], self.config["timeout_seconds"])
        result.content = result.content.encode()[-self.config["max_output_bytes"]:].decode(errors="replace")
        return result
