import json

from pluginrsi.search.feedback import fixed_solver_limits, published_plugin_evidence, summarize_trace
from pluginrsi.search.store import write_json


def test_summary_keeps_final_failure_and_source_without_repeated_requests(tmp_path):
    path = tmp_path / "trace.jsonl"
    source = {"kind": "tool", "plugin_ref": "tool/terminal/v0001"}
    events = [{"event": "plugin/loaded", "plugin_alias": "terminal", "plugin_ref": "tool/terminal/v0001", "provenance": {"label": "Terminal"}}]
    for number in range(20):
        events += [{"event": "model/request", "request": {"input": [
                       {"role": "user", "content": "Fix the reported boundary bug"},
                       {"role": "assistant", "content": "repeated context" * 1000}]}},
                   {"event": "tool/request", "command": f"check {number}", "producer": source, "invocation_id": str(number)},
                   {"event": "tool/response", "content": "output" * 1000 + "final failure", "invocation_id": str(number)}]
    events.append({"event": "model/response", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "I believe the fix is complete"}]}]}})
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    summary = summarize_trace(path, {"task_id": "task", "score": 0, "status": "completed"})
    assert summary["score"] == 0 and summary["model_calls"] == 20
    assert summary["first_model_user_input"] == "Fix the reported boundary bug"
    assert summary["commands"][-1]["output_tail"].endswith("final failure")
    assert summary["commands"][-1]["producer"] == source
    assert summary["omitted_commands"] == 8
    assert "repeated context" not in json.dumps(summary)
    assert summary["last_assistant_messages"] == ["I believe the fix is complete"]
    assert len(summary["command_timeline"]) == 20
    assert summary["command_timeline"][7]["command_preview"] == "check 7"
    assert summary["command_timeline"][7]["preceding_model_request_count"] == 8
    assert summary["omitted_timeline_commands"] == 0


def test_command_timeline_is_bounded_and_locates_original_events(tmp_path, monkeypatch):
    monkeypatch.setattr("pluginrsi.search.feedback.MAX_TIMELINE_COMMANDS", 2)
    monkeypatch.setattr("pluginrsi.search.feedback.MAX_COMMAND_PREVIEW_CHARS", 4)
    path = tmp_path / "trace.jsonl"
    events = [{"event": "tool/request", "command": command, "invocation_id": str(i)}
              for i, command in enumerate(["abcdef", "xy", "omitted"])]
    path.write_text("\n" + "\n".join(json.dumps(event) for event in events))
    summary = summarize_trace(path, {"task_id": "task", "score": 0, "status": "completed"})
    first, second = summary["command_timeline"]
    assert first == {"command_index": 1, "trace_line": 2,
                     "preceding_model_request_count": 0, "command_preview": "abcd",
                     "command_truncated": True, "invocation_id": "0"}
    assert second["command_preview"] == "xy" and not second["command_truncated"]
    assert summary["omitted_timeline_commands"] == 1


def test_published_evidence_excludes_unfinished_siblings_and_unavailable_refs(tmp_path):
    ref = "skill/debugging/v_c000001"
    phase = {"completed": True, "iteration": 1, "phase": "plugin_mutation",
             "slots": [{"id": "c000001", "parent": "c000000", "status": "rejected", "reason": "no_batch_improvement"}],
             "parent_evaluations": {"c000000": "e000001"},
             "published_plugins": [{"ref": ref, "candidate_id": "c000001", "evaluation_id": "e000002"}]}
    write_json(tmp_path / "iterations/iter_0001/plugin_mutation.json", phase)
    write_json(tmp_path / "evaluations/e000001/evaluation.json", {"id": "e000001", "mean_score": 1})
    write_json(tmp_path / "evaluations/e000002/evaluation.json", {"id": "e000002", "mean_score": 0.75})
    unfinished = {**phase, "completed": False, "iteration": 2}
    write_json(tmp_path / "iterations/iter_0002/plugin_mutation.json", unfinished)
    request = {"feedback_dir": str(tmp_path / "evaluations/e000003"), "allowed_plugin_refs": [ref]}
    result = published_plugin_evidence(request)
    assert len(result) == 1 and result[0]["plugin_ref"] == ref and result[0]["source_candidate"] == "c000001"
    request["allowed_plugin_refs"] = []
    assert published_plugin_evidence(request) == []


def test_solver_constraints_are_explicit_without_exporting_credentials(tmp_path):
    write_json(tmp_path / "config.yaml", {"solver": {"max_calls": 20, "max_output_tokens": 8192,
        "api_key_env": "PRIVATE_KEY_VARIABLE"}, "evaluation": {"task_timeout_seconds": 900}})
    result = fixed_solver_limits({"feedback_dir": str(tmp_path / "evaluations/e000001")})
    assert result["model_calls_per_task"] == 20
    assert result["task_timeout_seconds"] == 900
    assert "PRIVATE_KEY_VARIABLE" not in json.dumps(result)
