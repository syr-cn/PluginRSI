"""Compact projections of public solver traces for the proposer."""

import json
from pathlib import Path

import yaml

MAX_COMMANDS = 12
MAX_OUTPUT_CHARS = 2200
MAX_FINAL_CHARS = 4000
MAX_INPUT_CHARS = 12000
MAX_TIMELINE_COMMANDS = 80
MAX_COMMAND_PREVIEW_CHARS = 180


def summarize_trace(path: Path, score: dict) -> dict:
    commands = []
    responses = []
    plugins = []
    model_calls = 0
    first_user_input = ""
    timeline = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event["event"]
        if kind == "plugin/loaded":
            plugins.append({key: event.get(key) for key in ("plugin_alias", "plugin_ref", "provenance")})
        elif kind == "model/request":
            model_calls += 1
            if model_calls == 1:
                inputs = event.get("request", {}).get("input", [])
                if isinstance(inputs, str):
                    first_user_input = inputs
                else:
                    message = next((item for item in inputs if item.get("role") == "user"), {})
                    content = message.get("content", "")
                    first_user_input = content if isinstance(content, str) else "\n".join(
                        item.get("text", "") for item in content if item.get("type") in ("input_text", "text"))
        elif kind == "model/response":
            for item in event["response"].get("output", []):
                if item.get("type") == "message":
                    text = "\n".join(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
                    if text:
                        responses.append(text[-MAX_FINAL_CHARS:])
        elif kind == "tool/request":
            command = event.get("command", "")
            if len(timeline) < MAX_TIMELINE_COMMANDS:
                timeline.append({"command_index": len(commands) + 1,
                                 "trace_line": line_number,
                                 "preceding_model_request_count": model_calls,
                                 "command_preview": command[:MAX_COMMAND_PREVIEW_CHARS],
                                 "command_truncated": len(command) > MAX_COMMAND_PREVIEW_CHARS,
                                 "invocation_id": event.get("invocation_id")})
            commands.append({"command": event.get("command", "")[:MAX_OUTPUT_CHARS],
                             "producer": event.get("producer"), "invocation_id": event.get("invocation_id")})
        elif kind == "tool/response" and commands:
            invocation = event.get("invocation_id")
            target = next((entry for entry in reversed(commands)
                           if "output_tail" not in entry and entry.get("invocation_id") == invocation), None)
            if target is not None:
                content = event.get("content", "")
                target["output_tail"] = content[-MAX_OUTPUT_CHARS:]
                target["output_truncated"] = len(content) > MAX_OUTPUT_CHARS
    chosen = commands if len(commands) <= MAX_COMMANDS else commands[:2] + commands[-(MAX_COMMANDS - 2):]
    return {"task_id": score["task_id"], "score": score["score"], "status": score["status"],
            "first_model_user_input": first_user_input[:MAX_INPUT_CHARS],
            "first_model_user_input_truncated": len(first_user_input) > MAX_INPUT_CHARS,
            "model_calls": model_calls, "plugins": plugins, "command_count": len(commands),
            "commands": chosen, "omitted_commands": len(commands) - len(chosen),
            "command_timeline": timeline,
            "omitted_timeline_commands": len(commands) - len(timeline),
            "last_assistant_messages": responses[-2:],
            "scope": "Public solver trace only. Scores are authoritative; assistant success claims are not. Full trace remains available."}


def published_plugin_evidence(request: dict) -> list[dict]:
    root = Path(request["feedback_dir"]).parent.parent
    allowed = set(request["allowed_plugin_refs"])
    evidence = []
    for path in sorted((root / "iterations").glob("*/*.json")):
        phase = json.loads(path.read_text())
        if not phase.get("completed"):
            continue
        evidence.extend({"plugin_ref": p["ref"], "source_candidate": p["candidate_id"],
                         "branch": p.get("branch"), "batch_task_ids": p.get("batch_task_ids"),
                         "initial_parent_evaluation_id": p.get("initial_parent_evaluation_id"),
                         "local_results": [s for s in phase.get("plugin_evidence", [])
                                           if "branch" not in p or s.get("branch") == p["branch"]],
                         "interpretation": "Improvement on this branch's fixed failure/control batch; not comparable absolute accuracy across branches or full held-in accuracy."}
                        for p in phase["published_plugins"] if p["ref"] in allowed)
    return evidence


def fixed_solver_limits(request: dict) -> dict:
    root = Path(request["feedback_dir"]).parent.parent
    config = yaml.safe_load((root / "config.yaml").read_text())
    return {"model_calls_per_task": config["solver"]["max_calls"],
            "max_output_tokens_per_call": config["solver"]["max_output_tokens"],
            "task_timeout_seconds": config["evaluation"]["task_timeout_seconds"],
            "scope": "Shared across the workflow, plugin model calls and API retries. Raising a workflow turn limit does not raise these limits."}
