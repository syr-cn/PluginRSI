"""Configured API clients sharing the harness request and result contract."""

from abc import ABC, abstractmethod
import json

from openai import AsyncOpenAI
from jsonschema import ValidationError, validate

from .schemas import SolverConfig


class InvalidModelRequest(ValueError):
    """A deterministic request error that cannot be repaired by retrying."""


def validate_tool_calls(result, tools):
    if result.get('status') == 'failed' or result.get('error'):
        return result
    declared = {tool['name']: tool for tool in tools or [] if tool.get('type') == 'function'}
    seen = set()
    for call in result.get('output') or []:
        if not isinstance(call, dict) or call.get('type') != 'function_call':
            continue
        reason = None
        name, call_id = call.get('name'), call.get('call_id')
        if not isinstance(name, str) or name not in declared:
            reason = f'Model returned an undeclared tool name: {str(name)[:180]!r}'
        elif not isinstance(call_id, str) or not call_id or call_id in seen:
            reason = 'Model returned a missing or duplicate tool call ID'
        else:
            try:
                arguments = json.loads(call.get('arguments'))
                json.dumps(arguments, allow_nan=False)
                if not isinstance(arguments, dict):
                    reason = 'Model tool arguments must be a JSON object'
                else:
                    validate(arguments, declared[name].get('parameters', {}))
            except (TypeError, ValueError, ValidationError) as error:
                reason = f'Model returned invalid tool arguments: {str(error)[:180]}'
        if reason:
            return {**result, 'status': 'failed', 'error': {'code': 'invalid_tool_call', 'message': reason}}
        seen.add(call_id)
    return result


def response_schema(schema):
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
        if key in result:
            result[key] = {name: response_schema(value) for name, value in result[key].items()}
    for key in ('items', 'additionalProperties', 'contains', 'not', 'if', 'then', 'else', 'propertyNames'):
        if key in result:
            result[key] = response_schema(result[key])
    for key in ('anyOf', 'allOf', 'oneOf', 'prefixItems'):
        if key in result:
            result[key] = [response_schema(value) for value in result[key]]
    if 'oneOf' in result:
        if 'anyOf' in result:
            raise InvalidModelRequest('Responses schema combines oneOf and anyOf at the same node')
        seen = set()
        for branch in result['oneOf']:
            types = branch.get('type') if isinstance(branch, dict) else None
            types = {types} if isinstance(types, str) else set(types or [])
            if 'number' in types:
                types.add('integer')
            if not types or seen & types:
                raise InvalidModelRequest('Responses oneOf requires alternatives with disjoint JSON types')
            seen.update(types)
        result['anyOf'] = result.pop('oneOf')
    return result


class ModelTransport(ABC):
    def __init__(self, config: SolverConfig, emit, *, api_key: str, base_url: str):
        self.config = config
        self.emit = emit
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                                  timeout=config.timeout_seconds, max_retries=0)

    async def complete(self, messages: list, tools: list | None = None, *,
                       text_format: dict | None = None, **trace_context) -> dict:
        """Perform one attempt and return normalized output, text, status and usage.

        Traces retain the harness message format for replay. SDK exceptions
        propagate to the caller's shared retry and budget policy.
        """
        cfg = self.config
        request = dict(model=cfg.model, input=messages, max_output_tokens=cfg.max_output_tokens, store=False)
        if cfg.reasoning_effort is not None:
            request['reasoning'] = {'effort': cfg.reasoning_effort}
        if cfg.thinking_mode is not None:
            request['thinking'] = {'type': cfg.thinking_mode}
        if tools:
            request["tools"] = ([dict(tool, parameters=response_schema(tool["parameters"])) if "parameters" in tool else dict(tool)
                                 for tool in tools] if cfg.api_mode == "responses" else tools)
        if text_format is not None:
            request["text"] = {"format": text_format}
        self.emit("model/request", request=request, api_mode=cfg.api_mode, **trace_context)
        result = await self._complete(request)
        return validate_tool_calls(result, tools) if cfg.tool_call_validation == 'strict' else result

    @abstractmethod
    async def _complete(self, request: dict) -> dict:
        pass

    async def close(self):
        await self.client.close()


class ResponsesTransport(ModelTransport):
    async def _complete(self, request: dict) -> dict:
        response = await self.client.responses.create(**request)
        result = response.model_dump(mode="json")
        result["output_text"] = response.output_text
        return result


class ChatCompletionsTransport(ModelTransport):
    async def _complete(self, request: dict) -> dict:
        response = await self.client.chat.completions.create(**_chat_request(request, self.config.chat_json_mode))
        result = _chat_response(response)
        if (self.config.chat_json_mode == 'prompt' and request.get('text')
                and result['status'] == 'completed'
                and not any(item.get('type') == 'function_call' for item in result['output'])):
            try:
                value = json.loads(result['output_text'])
                json.dumps(value, allow_nan=False)
                valid = isinstance(value, dict)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                result.update(status='failed', error={'code': 'server_error',
                              'message': 'Model did not return a valid JSON object in prompt JSON mode'})
        return result


_TRANSPORTS = {"responses": ResponsesTransport, "chat_completions": ChatCompletionsTransport}


def create_model_transport(config: SolverConfig, emit, *, api_key: str, base_url: str) -> ModelTransport:
    return _TRANSPORTS[config.api_mode](config, emit, api_key=api_key, base_url=base_url)


def _chat_messages(messages):
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    result = []
    pending_calls = set()
    for item in messages:
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in pending_calls:
                raise InvalidModelRequest('Tool call history contains a missing or duplicate call_id')
            pending_calls.add(call_id)
            call = {"id": item["call_id"], "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]}}
            if result and result[-1]["role"] == "assistant":
                result[-1].setdefault("tool_calls", []).append(call)
            else:
                result.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in pending_calls:
                raise InvalidModelRequest('Tool output has no matching function_call in the request history')
            pending_calls.remove(call_id)
            result.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif "role" in item:
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part.get("type") not in ("input_text", "output_text", "text"):
                        raise ValueError(f"unsupported chat content type: {part.get('type')}")
                    parts.append({"type": "text", "text": part["text"]})
                content = parts
            result.append({"role": item["role"], "content": content})
        else:
            raise ValueError(f"unsupported harness message type: {kind}")
    return result


def _chat_request(request, json_mode='native'):
    result = {"model": request["model"], "messages": _chat_messages(request["input"]),
              "max_tokens": request["max_output_tokens"], "stream": False}
    effort = (request.get('reasoning') or {}).get('effort')
    if effort is not None:
        result['reasoning_effort'] = effort
    if request.get('thinking') is not None:
        result['extra_body'] = {'thinking': request['thinking']}
    if request.get("tools"):
        result["tools"] = [{"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}
                           for tool in request["tools"]]
    if request.get("text"):
        fmt = request["text"]["format"]
        if json_mode == 'prompt':
            if fmt['type'] != 'json_object':
                raise InvalidModelRequest('prompt JSON mode supports json_object only; JSON Schema is not emulated')
            result['messages'] = [{'role': 'system', 'content':
                'Use tools if needed. Any final text must be exactly one valid JSON object, without Markdown or surrounding text.'},
                *result['messages']]
        else:
            result["response_format"] = ({"type": "json_schema", "json_schema": {k: v for k, v in fmt.items() if k != "type"}}
                                         if fmt["type"] == "json_schema" else fmt)
    return result


def _chat_response(response):
    if not hasattr(response, "model_dump"):
        return {"status": "failed", "output": [], "output_text": "",
                "error": {"code": "invalid_response", "message": "Chat endpoint returned a non-JSON response"}}
    raw = response.model_dump(mode="json")
    choices = raw.get("choices") or []
    result = {"id": raw.get("id"), "model": raw.get("model"), "status": "completed",
              "output": [], "output_text": "", "error": raw.get("error")}
    usage = raw.get("usage") or {}
    result["usage"] = {"input_tokens": usage.get("prompt_tokens"),
                       "output_tokens": usage.get("completion_tokens"),
                       "total_tokens": usage.get("total_tokens"),
                       "input_tokens_details": usage.get("prompt_tokens_details"),
                       "output_tokens_details": usage.get("completion_tokens_details")} if raw.get("usage") is not None else None
    if not choices:
        result["status"] = "failed"
        result["error"] = result["error"] or {"code": "server_error", "message": "Chat endpoint returned no choices"}
        return result
    choice = choices[0]
    result["finish_reason"] = choice.get("finish_reason")
    message = choice["message"]
    text = message.get("content") or ""
    result["output_text"] = text
    if text:
        result["output"].append({"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": text}]})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
            result.update(status='failed', error={'code': 'invalid_tool_call', 'message': 'Malformed Chat tool-call envelope'})
            return result
        result["output"].append({"type": "function_call", "call_id": call.get("id"),
                                 "name": call['function'].get("name"), "arguments": call['function'].get("arguments")})
    if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
        result.update(status="failed", error={"code": "refusal", "message": "Model refused the request"})
    return result
