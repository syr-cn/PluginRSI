from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REF_PATTERN = r"^(role|tool|skill|memory)/[a-z][a-z0-9_]*/v[a-zA-Z0-9_]+$"
ENTRY_PATTERN = r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*$"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class PluginManifest(Schema):
    kind: Literal["role", "tool", "skill", "memory"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^v[a-zA-Z0-9_]+$")
    entrypoint: str = Field(pattern=ENTRY_PATTERN)
    description: str
    provenance: dict = Field(default_factory=dict)
    config_schema: dict = Field(default_factory=lambda: {"type": "object", "additionalProperties": False})

    @property
    def ref(self):
        return f"{self.kind}/{self.name}/{self.version}"


class PluginInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=REF_PATTERN)
    config: dict = Field(default_factory=dict)


class HarnessManifest(Schema):
    entrypoint: str = Field(pattern=ENTRY_PATTERN)
    plugins: dict[str, PluginInstance] = Field(default_factory=dict)

    @model_validator(mode="after")
    def aliases(self):
        if any(not re.fullmatch(r"[a-zA-Z_]\w*", key) for key in self.plugins):
            raise ValueError("plugin aliases must be Python identifiers")
        return self


class ProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str
    changes: list[str]
    new_plugin_refs: list[str]


class SearchOptions(BaseModel):
    """Search hyperparameters; paper symbols in brackets."""
    model_config = ConfigDict(extra="forbid")
    iterations: int = Field(default=1, ge=0)                     # [T]
    offspring_per_phase: int = Field(default=2, ge=1)            # [N] plugin-mutation branches per iteration
    feedback_batch_size: int = Field(default=2, ge=1)            # [b] minibatch size per branch
    local_failure_tasks: int = Field(default=1, ge=1)            # failed tasks in each balanced minibatch
    recomposition_offspring: int = Field(default=1, ge=1)        # harness candidates per recomposition
    local_branch_timeout_seconds: float | None = Field(default=None, gt=0)
    random_seed: int = 42
    max_task_rollouts: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def balanced(self):
        if self.local_failure_tasks >= self.feedback_batch_size:
            raise ValueError("balanced minibatches need both failed and solved task slots")
        return self


class SolverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    api_mode: Literal["responses", "chat_completions"] = "responses"
    api_metrics_path: Path | None = None
    api_key_env: str = "SOLVER_API_KEY"
    base_url_env: str = "SOLVER_BASE_URL"
    reasoning_effort: str | None = "none"
    thinking_mode: Literal['enabled', 'disabled'] | None = None
    chat_json_mode: Literal['native', 'prompt'] = 'native'
    tool_call_validation: Literal['strict', 'passthrough'] = 'strict'
    max_output_tokens: int = Field(default=8192, ge=1)
    max_calls: int = Field(default=30, ge=1)
    timeout_seconds: float = Field(default=180, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    retry_delay_seconds: float = Field(default=2, ge=0)
    rpm_limit: int | None = Field(default=None, ge=1)
    rate_limit_retry_seconds: float = Field(default=300.0, gt=0)
    adaptive_rate_limit: bool = False
    rpm_state_path: Path | None = None

    @model_validator(mode="after")
    def rate_limit(self):
        if self.thinking_mode is not None and self.api_mode != 'chat_completions':
            raise ValueError('thinking_mode requires chat_completions')
        if self.chat_json_mode != 'native' and self.api_mode != 'chat_completions':
            raise ValueError('chat_json_mode requires chat_completions')
        if (self.rpm_limit is None) != (self.rpm_state_path is None):
            raise ValueError("rpm_limit and rpm_state_path must be configured together")
        if self.adaptive_rate_limit and self.rpm_limit is None:
            raise ValueError("adaptive_rate_limit requires configured request admission")
        return self


class QABenchmarkOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_file: Path
    judge_config: Path
    math_timeout_seconds: float = Field(default=20, gt=0)
    math_startup_timeout_seconds: float = Field(default=120, gt=0)
    worker_python: str | None = None
    module_paths: list[str] = Field(default_factory=list)


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    benchmark: Literal["swe_harbor", "terminal_bench_2_1", "qa_transfer", "fixture"]
    feedback_tasks: Path
    selection_tasks: Path
    heldout_tasks: Path | None = None
    parallel_heldout: bool = False
    background_completion: bool = False
    search_ready_tasks: int | None = Field(default=None, ge=1)
    concurrency: int = Field(default=1, ge=1)
    min_valid_ratio: float = Field(default=1.0, gt=0, le=1)
    full_min_valid_ratio: float | None = Field(default=None, gt=0, le=1)
    infra_retry_attempts: int = Field(default=0, ge=0)
    accept_partial_results: bool = False
    eager_infra_retries: bool = False
    fail_fast_candidate_errors: bool = False
    local_timeout_seconds: float | None = Field(default=None, gt=0)
    evaluation_timeout_seconds: float | None = Field(default=None, gt=0)
    verifier_timeout_seconds: float | None = Field(default=None, gt=0)
    worker_initialization_timeout_seconds: float | None = Field(default=None, gt=0)
    task_timeout_seconds: float = Field(default=900, gt=0)
    infrastructure_timeout_seconds: float = Field(default=2400, gt=0)
    startup_stagger_min_seconds: float = Field(default=0, ge=0)
    startup_stagger_max_seconds: float = Field(default=0, ge=0)
    benchmark_options: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def stagger(self):
        if self.benchmark == 'qa_transfer':
            QABenchmarkOptions.model_validate(self.benchmark_options)
        if self.worker_initialization_timeout_seconds is not None and self.verifier_timeout_seconds is None:
            raise ValueError("staged worker deadlines require an explicit verifier timeout")
        if self.startup_stagger_max_seconds < self.startup_stagger_min_seconds:
            raise ValueError("invalid startup stagger range")
        if self.concurrency > 1 and self.startup_stagger_max_seconds == 0:
            raise ValueError("concurrent evaluation requires startup staggering")
        return self


class ProposerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1)
    concurrency: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=1800, gt=0)
    validation_timeout_seconds: float = Field(default=60, gt=0)
    validation_max_attempts: int = Field(default=1, ge=1)
    max_protocol_errors: int = Field(default=3, ge=1)
    budget_reserve_calls: int = Field(default=0, ge=0)
    context_max_bytes: int = Field(default=524288, ge=16384)
    compact_evidence: bool = False
    read_max_chars: int = Field(default=12000, ge=1000)
    read_batch_max_chars: int = Field(default=32000, ge=2000)
    evidence_top_k: int = Field(default=3, ge=1)
    agent: SolverConfig | None = None


class RunConfig(Schema):
    seed: Path
    plugin_library: Path
    run_dir: Path
    search: SearchOptions = Field(default_factory=SearchOptions)
    evaluation: EvaluationConfig
    solver: SolverConfig
    proposer: ProposerConfig

    @model_validator(mode='after')
    def heldout_mode(self):
        if self.evaluation.parallel_heldout and (
                self.evaluation.heldout_tasks is None):
            raise ValueError('parallel_heldout requires heldout_tasks')
        return self


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_config(path: Path) -> RunConfig:
    config = RunConfig.model_validate(read_yaml(path))
    root = path.resolve().parent
    for obj, names in [(config, ("seed", "plugin_library", "run_dir")),
                       (config.evaluation, ("feedback_tasks", "selection_tasks", "heldout_tasks"))]:
        for name in names:
            value = getattr(obj, name)
            if value is not None:
                setattr(obj, name, (root / value).resolve())
    if config.evaluation.benchmark == 'qa_transfer':
        from .evaluation.qa_data import resolve_qa_paths
        resolve_qa_paths(config.evaluation.benchmark_options, root)
    return config
