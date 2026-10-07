"""The same file-action agent with a schema-backed text transport and explicit fitness."""

import asyncio
from pathlib import Path
import sys

from .api_proposer import TOOLS
from .store import read_json, write_json
from .text_proposer import TextActionEvolver

ACTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["actions", "final"],
    "properties": {
        "final": {"type": "boolean"},
        "actions": {"type": "array", "items": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["name", "arguments"],
             "properties": {"name": {"type": "string", "enum": [tool["name"]]},
                            "arguments": tool["parameters"]}}
            for tool in TOOLS]}},
    },
}
FITNESS_CONTRACT = """Optimize actual trusted verifier scores under the fixed solver and task budgets.
A child enters selection only if its mean screening score strictly exceeds its own parent's score on the identical batch.
Complete fixed-selection means rank the archive; equal scores retain the older candidate. Token savings do not break ties.
Publication establishes executability, not a performance benefit. A useful reusable plugin can still be published without batch improvement.
Explain a concrete transferable mechanism supported by feedback. More cautious wording or fewer unsupported success claims alone is not a correctness gain.
Preserve useful solver capabilities. Additional restrictions on ordinary repository operations need a task-solving justification, not just a stylistic preference.
The proposer must not install host dependencies; candidate packages use the locked host SDK. Solver execution follows the configured benchmark environment and should use existing prepared tools/environments.
Do not hard-code task-ID-specific answers, tamper with hidden verifier tests or scoring, change model identity or resource ceilings, or read hidden evaluation inputs. Ordinary repository regression tests remain part of the solver's work.
"""


class StructuredActionEvolver(TextActionEvolver):
    transport_name = "structured_text_actions"
    response_format = {"type": "json_schema", "name": "file_actions", "schema": ACTION_SCHEMA, "strict": True}

    def prepare_context(self):
        public = super().prepare_context()
        public["fitness_contract"] = self.request.get("fitness_contract", FITNESS_CONTRACT)
        write_json(self.output / "proposal_context.json", public)
        return public


if __name__ == "__main__":
    asyncio.run(StructuredActionEvolver(read_json(Path(sys.argv[1]))).run())
