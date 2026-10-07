"""Optional coding-agent command using Responses and confined file tools."""

import asyncio
import ast
import json
import sys
from pathlib import Path

from ..runtime import EventLog, InfraError, SolverAgent
from ..schemas import ProposalResult, SolverConfig
from pydantic import ValidationError as SchemaValidationError
from jsonschema import ValidationError as JsonSchemaError
from yaml import YAMLError
from ..tracing import origin
from .store import read_json, write_json
from .feedback import fixed_solver_limits, published_plugin_evidence, summarize_trace

MAX_FILE_CHARS = 60000
TOOLS = [
    {"type": "function", "name": "submit_proposal", "description": "Write the required proposal_result.json with typed metadata after creating the candidate files.",
     "parameters": {"type": "object", "properties": {"hypothesis": {"type": "string"},
                    "changes": {"type": "array", "items": {"type": "string"}},
                    "new_plugin_refs": {"type": "array", "items": {"type": "string"}}},
                    "required": ["hypothesis", "changes", "new_plugin_refs"], "additionalProperties": False}},
    {"type": "function", "name": "read_file", "description": "Read a file in the supplied workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"type": "function", "name": "read_file_range", "description": "Read a character range, including later portions of long feedback traces.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_CHARS}},
                    "required": ["path", "offset", "max_chars"], "additionalProperties": False}},
    {"type": "function", "name": "write_file", "description": "Write an allowed child file in full.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"], "additionalProperties": False}},
    {"type": "function", "name": "list_files", "description": "List readable workspace files.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


class EvolverAgent:
    def __init__(self, request: dict):
        self.request = request
        self.output = Path(request["output_dir"])
        self.files = {}
        self.lazy_feedback = {}
        self.compact = request.get("compact_evidence", False)
        self.read_limit = min(MAX_FILE_CHARS, request.get("proposer_read_max_chars", MAX_FILE_CHARS))
        for label, root in [("child", self.output / "harness"), ("parent", Path(request["parent_dir"]))]:
            self.files.update({f"{label}/{p.relative_to(root)}": p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts})
        for ref in request["allowed_plugin_refs"]:
            for root in map(Path, request["library_dirs"]):
                package = root / ref
                if package.exists():
                    self.files.update({f"library/{ref}/{p.relative_to(package)}": p for p in package.rglob("*")
                                       if p.is_file() and "__pycache__" not in p.parts})
        feedback_root = Path(request["feedback_dir"])
        for row in request["feedback"]["scores"]:
            path = (feedback_root / row["trajectory"]).resolve()
            if path.is_relative_to(feedback_root.resolve()) and path.is_file():
                self.files[f"feedback/{row['task_id']}.jsonl"] = path
                if self.compact and request.get('phase') == 'harness_recomposition':
                    for suffix, filename in (('summary.json', 'proposer_compact_summary.json'), ('events.jsonl', 'proposer_events.jsonl')):
                        label = f"feedback/{row['task_id']}.{suffix}"
                        self.files[label] = path.with_name(filename)
                        self.lazy_feedback[label] = (path, row)
        for label, filename in request.get("extra_readable_files", {}).items():
            self.files[label] = Path(filename)
        for p in (self.output / "new_plugins").rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                self.files[str(p.relative_to(self.output))] = p

    def submission_error(self, result):
        if not self.request.get("validate_on_submit"):
            return None
        from .proposer import validate_proposal
        from .mutation import validate_local_surface
        try:
            refs = validate_proposal(self.output, self.request["candidate_id"], self.request["phase"],
                list(map(Path, self.request["library_dirs"])), self.request["allowed_plugin_refs"], result, load_code=False)
            for root in (self.output / "harness", self.output / "new_plugins"):
                for path in root.rglob("*.py"):
                    ast.parse(path.read_text(), filename=str(path.relative_to(self.output)))
            if "local_contract" in self.request:
                validate_local_surface(Path(self.request["parent_dir"]), self.output / "harness",
                    self.request["local_contract"]["alias"], refs[0])
            return None
        except (ValueError, OSError, SyntaxError, JsonSchemaError, YAMLError) as error:
            guidance = ("This phase uses frozen plugins: do not create plugin packages; submit new_plugin_refs=[]."
                        if self.request.get('phase') == 'harness_recomposition'
                        else "Plugin packages belong under new_plugins/, never library/ or child/plugins/.")
            return f"error: candidate is not ready: {error}. {guidance}"

    def tool(self, name, arguments):
        if name == "submit_proposal":
            return self.tool("write_file", {"path": "proposal_result.json", "content": json.dumps(arguments)})
        if name == "list_files":
            return json.dumps(sorted(self.files))
        path = arguments["path"]
        if name in ("read_file", "read_file_range"):
            if path not in self.files:
                return "error: file is not readable"
            if path in self.lazy_feedback:
                from .compact_evidence import trace_views
                trace, row = self.lazy_feedback[path]
                trace_views(trace, row)
            content = self.files[path].read_text()
            offset = arguments.get("offset", 0)
            size = arguments.get("max_chars", self.read_limit)
            if not isinstance(offset, int) or offset < 0 or not isinstance(size, int) or not 1 <= size <= MAX_FILE_CHARS:
                return "error: offset must be nonnegative and max_chars must be within the file-read limit"
            size = min(size, self.read_limit)
            end = min(offset + size, len(content))
            result = content[offset:end]
            if offset or end < len(content):
                result += f"\n[characters {offset}:{end} of {len(content)}; use read_file_range to inspect other ranges]"
            return result
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return "error: use a relative workspace path"
        if self.request.get("local_contract") and path.startswith("child/") and path != "child/harness.yaml":
            return f"error: local workflow files are frozen; edit {self.request['local_contract']['plugin_output_prefix']}/ instead"
        if self.request.get("local_contract") and path.startswith("new_plugins/") and not path.startswith(self.request["local_contract"]["plugin_output_prefix"] + "/"):
            return f"error: only the assigned plugin package is writable: {self.request['local_contract']['plugin_output_prefix']}/"
        if path.startswith("child/"):
            target = self.output / "harness" / relative.relative_to("child")
        elif path.startswith("new_plugins/") and self.request["phase"] == "plugin_mutation":
            target = self.output / relative
        elif path == "proposal_result.json":
            target = self.output / relative
            try:
                value = json.loads(arguments["content"])
                ProposalResult.model_validate(value)
            except (json.JSONDecodeError, SchemaValidationError) as error:
                return f"error: proposal_result.json requires hypothesis:string, changes:list[string], new_plugin_refs:list[string]: {error}"
            error = self.submission_error(value)
            if error:
                return error
        else:
            return "error: library/ is read-only; new plugin files must use new_plugins/<kind>/<name>/<assigned_version>/"
        if not target.resolve().is_relative_to(self.output.resolve()):
            return "error: path escapes candidate directory"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(arguments["content"])
        self.files[path] = target
        return "written"

    async def run(self):
        with origin({"producer": {"kind": "proposer"}, "candidate_id": self.request["candidate_id"],
                     "phase": self.request["phase"], "iteration": self.request.get("iteration"),
                     "model_role": "evolver"}):
            try:
                return await self._run()
            except InfraError as error:
                write_json(self.output / "proposal_failure.json", {"kind": "infra_error", "detail": str(error)})
                raise

    def prepare_context(self):
        summaries = []
        representatives = None
        if self.compact and self.request['phase'] == 'harness_recomposition':
            from .compact_evidence import representative_ids
            representatives = representative_ids(self.request['feedback']['scores'])
        for row in self.request["feedback"]["scores"]:
            if representatives is not None and row['task_id'] not in representatives:
                continue
            trace = self.files.get(f"feedback/{row['task_id']}.jsonl")
            if trace is not None and trace.is_file():
                if self.compact:
                    from .compact_evidence import trace_views
                    summary, events, data = trace_views(trace, row)
                    self.files[f"feedback/{row['task_id']}.events.jsonl"] = events
                    summaries.append(data)
                else:
                    summary = self.output / "feedback_summaries" / f"{row['task_id']}.json"
                    write_json(summary, summarize_trace(trace, row))
                self.files[f"feedback/{row['task_id']}.summary.json"] = summary
        public = {key: self.request[key] for key in ("phase", "candidate_id", "version", "allowed_plugin_refs", "history", "contracts", "runtime_contract", "instructions")}
        public["feedback_scores"] = [{k: row[k] for k in ("task_id", "score", "status")} for row in self.request["feedback"]["scores"]]
        public["feedback_reading"] = "Start with feedback/*.summary.json, then read relevant full trace ranges and plugin code to connect a concrete failure to a proposed mechanism. Compare available parent/child evidence and check successful cases for regressions. Cite task IDs and trace locations in hypothesis. A failed verifier score overrides the solver's claim that its patch works."
        public["published_plugin_evidence"] = published_plugin_evidence(self.request)
        public["publication_meaning"] = "Publication proves executability only. Use completed-phase matched comparisons to avoid blindly reusing regressed mechanisms. These results concern full harnesses, not isolated plugin effects."
        if "fitness_contract" in self.request:
            public["publication_meaning"] = "Published local plugins passed strict local-improvement selection. Prioritize their observed gains when designing integration, while checking regressions and applicability. Failure/control batches do not establish full-held-in gains or universal plugin effects."
        public["fixed_solver_limits"] = fixed_solver_limits(self.request)
        for key in ("local_contract", "local_evidence", "fitness_contract"):
            if key in self.request:
                public[key] = self.request[key]
        if self.compact:
            from .compact_evidence import prepare_compact
            public = prepare_compact(self, public, summaries)
        write_json(self.output / "proposal_context.json", public)
        return public

    def initial_files(self):
        if not self.compact:
            return sorted(self.files)
        return sorted(name for name in self.files if name.startswith(('child/', 'parent/', 'new_plugins/'))
                      or name.endswith('change.diff')
                      or (self.request.get('local_contract') and name.endswith('.summary.json'))
                      or (name.startswith('library/') and name.endswith('/plugin.yaml')))

    async def _run(self):
        public = self.prepare_context()
        model = SolverAgent(SolverConfig.model_validate(self.request["proposer_agent"]),
                            EventLog(self.output / "proposer_trace.jsonl"))
        messages = [{"role": "system", "content": self.request["instructions"]},
                    {"role": "user", "content": json.dumps(public) + "\nFiles:\n" + json.dumps(self.initial_files())}]
        try:
            while True:
                response = await model.complete(messages, TOOLS)
                messages.extend(response["output"])
                calls = [item for item in response["output"] if item["type"] == "function_call"]
                if not calls:
                    if not (self.output / "proposal_result.json").exists():
                        raise ValueError("proposer stopped without proposal_result.json")
                    return
                for call in calls:
                    result = self.tool(call["name"], json.loads(call["arguments"]))
                    messages.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})
        finally:
            write_json(self.output / "proposer_usage.json", {"model_calls": model.calls, "tokens": model.tokens})
            await model.close()


if __name__ == "__main__":
    asyncio.run(EvolverAgent(read_json(Path(sys.argv[1]))).run())
