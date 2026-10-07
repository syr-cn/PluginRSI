from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from pathlib import Path
from email.utils import parsedate_to_datetime

from openai import APIConnectionError, APIStatusError, APITimeoutError

from .contracts import ToolResult
from .schemas import SolverConfig
from .tracing import ORIGIN, json_default
from .rate_limit import acquire
from .model_transports import InvalidModelRequest, create_model_transport

RATE_LIMIT_INITIAL_DELAY_SECONDS = 10.0
RATE_LIMIT_MAX_DELAY_SECONDS = 60.0


class InfraError(RuntimeError):
    pass


class InvalidToolCall(ValueError):
    pass


class ModelRequestRejected(InfraError):
    retryable = False


def policy_rejection(error):
    text = str(error).lower()
    return any(marker in text for marker in (
        'content_policy_violation', 'content_filter', 'safety_violation',
        'violating our usage policy', 'violates our usage policy',
    ))


class BudgetExceeded(RuntimeError):
    pass


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: str, **data):
        with self.path.open("a") as stream:
            stream.write(json.dumps({"event": event, "timestamp": time.time(), **data, **ORIGIN.get()}, ensure_ascii=False, default=json_default) + "\n")


class SolverAgent:
    """Shared retry, rate-limit and call-budget policy for solvers and evolvers."""

    def __init__(self, config: SolverConfig, emit):
        self.config = config
        self.emit = emit
        self.calls = 0
        self.failed_calls = 0
        self.request_attempts = 0
        self.tokens = 0
        self.plugin_outputs = []
        self.instance_id = uuid.uuid4().hex
        self.metrics = EventLog(config.api_metrics_path) if config.api_metrics_path else None
        key, url = os.environ.get(config.api_key_env), os.environ.get(config.base_url_env)
        if not key or not url:
            raise InfraError(f"set {config.api_key_env} and {config.base_url_env}")
        self.transport = create_model_transport(config, emit, api_key=key, base_url=url)

    async def complete(self, messages: list, tools: list | None = None, *, text_format: dict | None = None) -> dict:
        budget = asyncio.timeout(None)
        try:
            async with budget:
                return await self._complete(messages, tools, text_format=text_format, rate_budget=budget)
        except TimeoutError as error:
            if not budget.expired():
                raise
            raise InfraError("rate-limit retry deadline exhausted") from error

    async def _complete(self, messages, tools, *, text_format, rate_budget):
        cfg = self.config
        attempt = 0
        limited_attempts = 0

        async def retry_rate_limit(header=None):
            nonlocal limited_attempts
            self.calls -= 1
            if rate_budget.when() is None:
                rate_budget.reschedule(asyncio.get_running_loop().time() + cfg.rate_limit_retry_seconds)
            delay = min(RATE_LIMIT_MAX_DELAY_SECONDS, RATE_LIMIT_INITIAL_DELAY_SECONDS * 2 ** min(limited_attempts, 3))
            if header:
                try:
                    delay = max(delay, float(header))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(header).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            limited_attempts += 1
            delay += random.uniform(0, cfg.retry_delay_seconds)
            self.emit('model/rate_limit_retry', retry=limited_attempts, delay_seconds=delay,
                      deadline_seconds=cfg.rate_limit_retry_seconds, configured_rpm=cfg.rpm_limit)
            await asyncio.sleep(delay)

        while attempt < cfg.max_attempts:
            retry_after = 0.0
            if self.calls >= cfg.max_calls:
                if attempt or self.failed_calls:
                    raise InfraError("model API retries exhausted the remaining call budget")
                raise BudgetExceeded("solver model-call budget exhausted")
            if cfg.rpm_limit is not None:
                await acquire(cfg.rpm_state_path, cfg.rpm_limit)
            self.calls += 1
            self.request_attempts += 1
            call = self.request_attempts
            request_id = f"{self.instance_id}:{call}"
            started = time.monotonic()
            if self.metrics:
                self.metrics("api/request_start", request_id=request_id, model=cfg.model,
                             api_mode=cfg.api_mode, attempt=attempt, pid=os.getpid())
            try:
                result = await self.transport.complete(
                    messages, tools, text_format=text_format, call=call, attempt=attempt,
                    available_plugin_outputs=list(self.plugin_outputs))
            except InvalidModelRequest:
                self.failed_calls += 1
                self.request_outcome(request_id, call, started, False, 'invalid_prompt', None)
                raise
            except (APIConnectionError, APITimeoutError, APIStatusError) as error:
                self.failed_calls += 1
                status = getattr(error, "status_code", None)
                self.emit("model/error", call=call, error_type=type(error).__name__, status_code=status,
                          cause_type=type(error.__cause__).__name__ if error.__cause__ else None)
                self.request_outcome(request_id, call, started, False, type(error).__name__, status)
                if status == 429:
                    await retry_rate_limit(getattr(error.response, 'headers', {}).get('retry-after'))
                    continue
                if status in (400, 403, 422):
                    raise ModelRequestRejected(f"model API rejected this task: {error}") from error
                retryable = status is None or status in (408, 409, 429) or status >= 500
                if not retryable or attempt + 1 == cfg.max_attempts:
                    raise InfraError(f"model API failed: {type(error).__name__}, status={status}") from error
                header = getattr(getattr(error, "response", None), "headers", {}).get("retry-after", "0")
                try:
                    retry_after = min(180.0, max(0.0, float(header)))
                except ValueError:
                    retry_after = 0.0
            else:
                output_text = result["output_text"]
                self.tokens += (result.get("usage") or {}).get("total_tokens") or 0
                self.emit("model/response", call=call, response=result, total_tokens=self.tokens)
                has_output = bool(output_text.strip()) or any(item.get("type") == "function_call" for item in result["output"])
                failure = (result.get("error") or {}).get("code") or ("failed_response" if result.get("error") or result.get("status") == "failed" else None)
                self.request_outcome(request_id, call, started, not failure and has_output,
                                     failure or (None if has_output else "empty_response"), 200, result.get("usage"))
                if result.get("error") or result.get("status") == "failed":
                    error = result.get("error") or {}
                    reason = error.get("code")
                    if reason == 'invalid_tool_call':
                        raise InvalidToolCall(f"invalid_tool_call: {error.get('message', '')}")
                    self.failed_calls += 1
                    if policy_rejection(error):
                        raise ModelRequestRejected(f"model API rejected this task: {error}")
                    if reason in ('invalid_prompt', 'invalid_request_error', 'invalid_function_parameters', 'invalid_json_schema', 'context_length_exceeded'):
                        raise InvalidModelRequest(f"model request rejected: {reason}: {error.get('message', '')}")
                    if reason == 'rate_limit_exceeded':
                        await retry_rate_limit()
                        continue
                    if reason not in ("server_error", "rate_limit_exceeded") or attempt + 1 == cfg.max_attempts:
                        raise InfraError(f"model response failed: {error.get('code', 'unknown')}: {error.get('message', '')}")
                    self.emit("model/retry", call=call, reason=reason, next_attempt=attempt + 1)
                elif has_output:
                    return result
                else:
                    self.failed_calls += 1
                    if attempt + 1 == cfg.max_attempts:
                        raise InfraError("model API returned only empty responses")
            await asyncio.sleep(max(retry_after, cfg.retry_delay_seconds * (2 ** attempt))
                                + random.uniform(0, cfg.retry_delay_seconds))
            attempt += 1
        raise AssertionError("unreachable")

    def request_outcome(self, request_id, call, started, success, error_type, status_code, usage=None):
        data = dict(request_id=request_id, call=call, model=self.config.model,
                    api_mode=self.config.api_mode, success=success, error_type=error_type,
                    status_code=status_code, elapsed_seconds=round(time.monotonic() - started, 3), usage=usage)
        self.emit("model/request_outcome", **data)
        if self.metrics:
            self.metrics("api/request_end", **data)

    async def close(self):
        await self.transport.close()


class EnvironmentService:
    def __init__(self, environment, emit, command_prelude=""):
        self._environment = environment
        self.emit = emit
        self.command_prelude = command_prelude

    async def execute(self, command: str, timeout_seconds: float = 180) -> ToolResult:
        self.emit("tool/request", command=command, timeout_seconds=timeout_seconds)
        if self.command_prelude:
            command = f"{self.command_prelude} && (\n{command}\n)"
        try:
            result = await self._environment.exec(command, timeout_sec=timeout_seconds)
        except Exception as error:
            raise InfraError(f"environment command failed: {type(error).__name__}") from error
        content = f"exit={result.return_code}\nstdout={result.stdout or ''}\nstderr={result.stderr or ''}"
        self.emit("tool/response", content=content)
        return ToolResult(content, result.return_code != 0)

    async def upload_file(self, source: Path, destination: str):
        self.emit("environment/upload", source=str(source), destination=destination)
        await self._environment.upload_file(source, destination)

    async def download_file(self, source: str, destination: Path):
        await self._environment.download_file(source, destination)
        self.emit("environment/download", source=source, destination=str(destination))


class Runtime:
    def __init__(self, model, environment, emit, resource_dir: Path, work_dir: Path):
        self.model = model
        self.environment = environment
        self.emit = emit
        self.resource_dir = resource_dir
        self.work_dir = work_dir
        self.plugin_calls = 0
        self.plugin_outputs = []
        self.model.plugin_outputs = self.plugin_outputs
