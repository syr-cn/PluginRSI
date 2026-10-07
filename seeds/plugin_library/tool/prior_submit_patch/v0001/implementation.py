import json
import shlex

from jsonschema import validate
import yaml

from pluginrsi.contracts import Tool, ToolResult


class PriorTool(Tool):
    def schema(self) -> dict:
        return json.loads((self.services.resource_dir / "schema.json").read_text())

    async def execute(self, arguments: dict) -> ToolResult:
        validate(arguments, self.schema()["parameters"])
        spec = yaml.safe_load((self.services.resource_dir / "asset.yaml").read_text())
        name = spec["name"]
        if name == "submit_patch":
            if arguments["evidence_summary"].get("completion_decision") != "submit":
                return ToolResult("completion_decision must equal submit", is_error=True)
            record = {"instance_id": arguments["task_id"], "model_patch": arguments["patch"],
                      "model_name_or_path": arguments["workflow_version"],
                      "evidence_summary": arguments["evidence_summary"]}
            path = self.services.state_dir / "prediction.json"
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n")
            return ToolResult(json.dumps({"prediction_record": record, "artifact_path": str(path)}),
                              artifacts=[str(path)])
        script = (self.services.resource_dir / "sandbox.py").read_text()
        payload = {"name": name, "arguments": arguments, "config": self.config}
        command = '"${PLUGINRSI_HELPER_PYTHON:-python3}" -c ' + shlex.quote(script) + " " + shlex.quote(json.dumps(payload))
        timeout = min(arguments.get("timeout_seconds", self.config.get("timeout_seconds", 180)),
                      self.config.get("timeout_seconds", 180))
        result = await self.services.environment.execute(command, timeout + 5)
        return result
