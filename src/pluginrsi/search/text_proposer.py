"""File-action coding agent over ordinary text Responses, without API tool fields."""

import asyncio
import json
import sys
from pathlib import Path

from jsonschema import ValidationError, validate

from ..runtime import EventLog, InfraError, SolverAgent
from ..schemas import SolverConfig
from .api_proposer import EvolverAgent, TOOLS
from .store import read_json, write_json

MAX_PROTOCOL_ERRORS = 3
MAX_CONTEXT_BYTES = 524288
READ_PREVIEW_CHARS = 512
READ_ACTIONS = {'read_file', 'read_file_range', 'list_files'}
HISTORY_NOTICE = ('Earlier interactions were omitted to fit the context budget. Candidate files retain completed edits; '
                  'all allowed evidence remains readable. Re-read relevant files or ranges before revising them.')
ACTION_SCHEMAS = {tool["name"]: tool["parameters"] for tool in TOOLS}
ENVELOPE_SCHEMA = {
    "type": "object", "required": ["actions", "final"], "additionalProperties": False,
    "properties": {
        "actions": {"type": "array", "items": {"type": "object", "required": ["name", "arguments"],
            "additionalProperties": False, "properties": {"name": {"enum": list(ACTION_SCHEMAS)}, "arguments": {"type": "object"}}}},
        "final": {"type": "boolean"},
    },
}
PROTOCOL = """Use the same confined file actions described below, but encode them in ordinary text.
Every response must be one JSON object: {"actions":[{"name":"read_file","arguments":{"path":"child/workflow.py"}}],"final":false}.
You may batch actions. Their results arrive as quoted data in the next user message.
Use final=true only after delivering all executable files and proposal_result.json.
Use submit_proposal for final metadata. Each changes entry must be a string, not an object.
Never emit API function calls, shell commands as actions, prose outside JSON, or a mere diff description.
File actions have exactly the same read/write scope as the supplied coding-agent contract.
"""


def parse_envelope(text: str) -> dict:
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[len("```json\n"):-3].strip()
    envelope = json.loads(text)
    validate(envelope, ENVELOPE_SCHEMA)
    for action in envelope["actions"]:
        validate(action["arguments"], ACTION_SCHEMAS[action["name"]])
    return envelope


def context_size(messages):
    return len(json.dumps(messages, ensure_ascii=True).encode('utf-8'))


def bound_context(messages, limit):
    before = context_size(messages)
    if before <= limit:
        return messages, None
    bounded = list(messages)
    shortened = dropped = 0
    for index in range(2, len(bounded)):
        message = bounded[index]
        if message.get('role') != 'user':
            continue
        try:
            payload = json.loads(message['content'])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get('action_results'), list):
            continue
        changed = False
        for item in payload['action_results']:
            value = item.get('result')
            if (item.get('name') in READ_ACTIONS and isinstance(value, str)
                    and len(value) > 2 * READ_PREVIEW_CHARS + 512 and not value.startswith('error:')):
                item['result'] = (value[:READ_PREVIEW_CHARS] + '\n[Read output excerpted for context capacity; '
                    f'original length {len(value)} characters. Request fewer files or narrower read_file_range ranges '
                    'to inspect omitted content.]\n' + value[-READ_PREVIEW_CHARS:])
                shortened += 1
                changed = True
        if changed:
            bounded[index] = dict(message, content=json.dumps(payload))
        if context_size(bounded) <= limit:
            break

    def with_notice():
        return bounded[:2] + [{'role': 'user', 'content': HISTORY_NOTICE}] + bounded[2:] if dropped else bounded

    while context_size(with_notice()) > limit and len(bounded) > 4:
        del bounded[2:4]
        dropped += 2
    result = with_notice()
    after = context_size(result)
    if after > limit:
        raise InfraError(f'Protected proposer context and latest exchange exceed context_max_bytes={limit}; '
                         'reduce initial evidence or the latest read batch')
    return result, dict(bytes_before=before, bytes_after=after, shortened_reads=shortened, dropped_messages=dropped)


class TextActionEvolver(EvolverAgent):
    response_format = None
    transport_name = "text_actions"

    async def _run(self):
        public = self.prepare_context()
        emit = EventLog(self.output / "proposer_trace.jsonl")
        model = SolverAgent(SolverConfig.model_validate(self.request["proposer_agent"]), emit)
        public["file_actions"] = TOOLS
        public["transport"] = self.transport_name
        context_limit = self.request.get('proposer_context_max_bytes', MAX_CONTEXT_BYTES)
        public['context_management'] = {'max_encoded_bytes': context_limit,
            'policy': 'Older or oversized read results may be excerpted; files and full evidence remain readable. Batch fewer reads when excerpts omit needed content.'}
        write_json(self.output / "proposal_context.json", public)
        initial_context = json.dumps(public) + "\nFiles:\n" + json.dumps(self.initial_files())
        messages = [{"role": "system", "content": self.request["instructions"] + "\n" + PROTOCOL},
                    {"role": "user", "content": initial_context}]
        errors = 0
        error_limit = self.request.get('proposer_max_protocol_errors', MAX_PROTOCOL_ERRORS)
        reserve_calls = self.request.get('proposer_budget_reserve_calls', 0)
        max_calls = self.request['proposer_agent'].get('max_calls', SolverConfig.model_fields['max_calls'].default)
        try:
            while True:
                if reserve_calls:
                    remaining = max(0, max_calls - model.calls)
                    notice = f'Proposer model-call budget: {remaining}/{max_calls} calls remaining; retries may consume calls.'
                    if remaining <= reserve_calls:
                        notice += (' Finish implementation, check the candidate files, and submit proposal_result.json now. '
                                   'Avoid further broad evidence reading; reserve calls for repairs. '
                                   'Every response still requires actions and final; set final=true only with a valid submission.')
                    # Keep one live notice in the protected prefix, rather than growing history each turn.
                    messages[1] = dict(messages[1], content=initial_context + '\n' + notice)
                messages, compaction = bound_context(messages, context_limit)
                if compaction:
                    emit('proposer/context_compacted', **compaction)
                if self.response_format is None:
                    response = await model.complete(messages)
                else:
                    response = await model.complete(messages, text_format=self.response_format)
                text = response["output_text"]
                messages.append({"role": "assistant", "content": text})
                try:
                    envelope = parse_envelope(text)
                except (ValueError, ValidationError) as error:
                    errors += 1
                    emit("proposer/protocol_error", error_type=type(error).__name__, message=str(error))
                    if errors >= error_limit:
                        raise ValueError("text-action proposer exhausted protocol corrections") from error
                    messages.append({"role": "user", "content": f"Invalid action envelope: {error}. Return only a corrected JSON object."})
                    continue
                results = []
                remaining_read = self.request.get('proposer_read_batch_max_chars')
                for action in envelope["actions"]:
                    if action['name'] in READ_ACTIONS and remaining_read is not None and remaining_read < 512:
                        result = 'error: read batch budget exhausted; select fewer sources or narrower ranges next turn'
                    else:
                        arguments = dict(action['arguments'])
                        if action['name'] in ('read_file', 'read_file_range') and remaining_read is not None:
                            arguments['max_chars'] = min(arguments.get('max_chars', self.read_limit), remaining_read)
                        result = self.tool(action["name"], arguments)
                        if action['name'] in READ_ACTIONS and remaining_read is not None:
                            if len(str(result)) > remaining_read:
                                result = str(result)[:max(0, remaining_read-120)] + '\n[Read batch excerpted; use narrower requests.]'
                            remaining_read -= len(str(result))
                    item = {"name": action["name"], "result": result}
                    if action['name'] in READ_ACTIONS:
                        item['source'] = action['arguments']
                    results.append(item)
                    emit("proposer/file_action", action=action, result=result)
                if envelope["final"]:
                    failed_actions = [item for item in results if str(item["result"]).startswith("error:")]
                    result_path = self.output / "proposal_result.json"
                    if result_path.exists():
                        error = self.submission_error(read_json(result_path))
                        if error:
                            failed_actions.append({"name": "validate_submission", "result": error})
                    if not (self.output / "proposal_result.json").exists() or failed_actions:
                        errors += 1
                        message = "Finalization was not accepted. Resolve failed file actions and use write_file with path exactly proposal_result.json containing hypothesis, changes, and new_plugin_refs before finalizing."
                        emit("proposer/incomplete_final", errors=errors, failed_actions=failed_actions)
                        if errors >= error_limit:
                            raise ValueError("text-action proposer exhausted finalization corrections")
                        messages.append({"role": "user", "content": json.dumps({"action_results": results, "completion_error": message})})
                        continue
                    return
                messages.append({"role": "user", "content": json.dumps({"action_results": results})})
        finally:
            write_json(self.output / "proposer_usage.json", {"model_calls": model.calls, "tokens": model.tokens})
            await model.close()


if __name__ == "__main__":
    asyncio.run(TextActionEvolver(read_json(Path(sys.argv[1]))).run())
