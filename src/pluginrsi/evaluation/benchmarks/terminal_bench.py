import math
from pathlib import Path

from ...search.store import read_json, write_json


async def evaluate_harbor(request: dict) -> dict:
    """Use Harbor's task environment and verifier with a free-form candidate agent."""
    work = Path(request["work_dir"])
    staged = request['evaluation'].get('worker_initialization_timeout_seconds') is not None
    if staged:
        from ..progress import mark, milestone
        milestone(work, 'harbor_import_started')
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.trial import Trial

    if staged:
        milestone(work, 'harbor_import_finished')
    options = request["evaluation"]["benchmark_options"]
    environment = dict(options.get("environment", {"type": "docker"}))
    # Resource allocation is fixed by the evaluator, never by a proposed plugin.
    environment["override_gpus"] = 0
    verifier_options = {}
    if request["evaluation"].get("verifier_timeout_seconds") is not None:
        from harbor.models.trial.config import VerifierConfig
        verifier_options["verifier"] = VerifierConfig(override_timeout_sec=request["evaluation"]["verifier_timeout_seconds"])
    config = TrialConfig(
        task=TaskConfig(path=Path(request["task"]["task_path"])),
        trial_name="trial", trials_dir=work / "harbor",
        agent=AgentConfig(import_path="pluginrsi.evaluation.benchmarks.harbor_agent:HarnessAgent",
                          model_name=request["solver"]["model"],
                          override_timeout_sec=request["evaluation"]["task_timeout_seconds"],
                          kwargs={"harness_path": request["harness"], "libraries": request["libraries"],
                                  "solver": request["solver"], "task_id": request["task"]["id"], "work_dir": str(work),
                                  "command_prelude": options.get("command_prelude", "")}),
        environment=EnvironmentConfig(**environment),
        **verifier_options,
    )
    if staged:
        milestone(work, 'trial_create_started')
    trial = await Trial.create(config)
    if staged:
        from harbor.trial.hooks import TrialEvent
        milestone(work, 'trial_create_finished')
        async def initialized(event):
            milestone(work, 'trial_initialized')
        trial.add_hook(TrialEvent.START, initialized)
        for event, stage in ((TrialEvent.ENVIRONMENT_START, 'environment'),
                             (TrialEvent.AGENT_START, 'solving'),
                             (TrialEvent.AGENT_END, 'preparing_verifier'),
                             (TrialEvent.VERIFICATION_START, 'verifying'),
                             (TrialEvent.END, 'done')):
            async def transition(event, stage=stage):
                mark(work, stage)
            trial.add_hook(event, transition)
    result = (await trial.run()).model_dump(mode="json")
    write_json(work / "harbor_result.json", result)
    agent_path = work / "agent_status.json"
    agent = read_json(agent_path) if agent_path.exists() else {}
    exception = result.get("exception_info") or {}
    status = agent.get("status", "completed")
    if status == 'solver_error' and agent.get('detail') == 'invalid_tool_call':
        return {"score": 0.0, "status": "completed", "detail": "invalid_tool_call",
                "retryable": False, "failure_origin": "solver", "agent_error": agent}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward_key = options.get("reward_key", "reward")
    reward = rewards.get(reward_key)
    if (isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward)
            and status != 'candidate_error'):
        row = {"score": float(reward), "status": "solver_limit" if status == 'solver_limit' else "completed"}
        if exception:
            row['post_verification_error'] = exception
        if status == 'infra_error':
            row['agent_error'] = agent
        return row
    if exception:
        exception_type = exception.get("exception_type", "")
        return {"score": 0.0, "status": status if status in ("candidate_error", "infra_error") else "infra_error",
                "detail": exception_type,
                "retryable": agent.get('retryable', True),
                "failure_origin": "candidate" if status == "candidate_error" else "verifier_timeout_unattributed" if "VerifierTimeout" in exception_type else "solver_timeout" if "AgentTimeout" in exception_type else "benchmark_infrastructure"}
    if status == 'candidate_error':
        return {"score": 0.0, "status": status}
    if reward_key not in rewards:
        return {"score": 0.0, "status": "infra_error", "detail": "missing verifier reward"}
    return {"score": 0.0, "status": "infra_error", "detail": "invalid verifier reward"}
