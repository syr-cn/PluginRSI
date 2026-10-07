from dataclasses import asdict
from pathlib import Path

from harbor.agents.base import BaseAgent

from ...contracts import SolveResult, Task
from ...loader import execute
from ...runtime import BudgetExceeded, EnvironmentService, EventLog, InfraError, InvalidToolCall, Runtime, SolverAgent
from ...schemas import SolverConfig
from ...search.store import write_json


class HarnessAgent(BaseAgent):
    def __init__(self, *args, harness_path, libraries, solver, task_id, work_dir, command_prelude="", **kwargs):
        super().__init__(*args, **kwargs)
        self.harness_path = Path(harness_path)
        self.libraries = [Path(path) for path in libraries]
        self.solver_config = SolverConfig.model_validate(solver)
        self.task_id = task_id
        self.work_dir = Path(work_dir)
        self.command_prelude = command_prelude

    @staticmethod
    def name():
        return "pluginrsi"

    def version(self):
        return "0.1.0"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        emit = EventLog(self.work_dir / "trajectory.jsonl")
        model = SolverAgent(self.solver_config, emit)
        runtime = Runtime(model, EnvironmentService(environment, emit, self.command_prelude), emit,
                          self.harness_path, self.work_dir)
        status = "completed"
        try:
            result = await execute(self.harness_path, self.libraries, Task(self.task_id, instruction), runtime)
            if not isinstance(result, SolveResult):
                raise TypeError("Workflow.run must return SolveResult")
        except BudgetExceeded:
            status = "solver_limit"
            result = SolveResult("Model-call budget exhausted", termination_reason="model_limit")
        except InvalidToolCall as error:
            write_json(self.work_dir / "agent_status.json", {
                "status": "solver_error", "detail": "invalid_tool_call", "error": str(error), "retryable": False,
            })
            raise
        except InfraError as error:
            write_json(self.work_dir / "agent_status.json", {
                "status": "infra_error", "error": repr(error),
                "retryable": getattr(error, 'retryable', True),
            })
            raise
        except Exception as error:
            write_json(self.work_dir / "agent_status.json", {"status": "candidate_error", "error": repr(error)})
            raise
        finally:
            usage = {"model_calls": model.calls, "solver_tokens": model.tokens, "model_api_failures": model.failed_calls}
            emit("budget/used", **usage)
            write_json(self.work_dir / "usage.json", usage)
            await model.close()
        write_json(self.work_dir / "solve_result.json", asdict(result))
        write_json(self.work_dir / "agent_status.json", {"status": status})
