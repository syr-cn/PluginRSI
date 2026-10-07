---
name: pluginrsi-extend
description: Build new research on top of PluginRSI - write new plugins or seed harnesses, add a benchmark, or change the optimization algorithm (new search stage, selection rule, proposer instructions). Use when the user wants to implement, modify or ablate a harness-optimization method in this repository.
---

# Extend PluginRSI

## Mental model (paper §3, Algorithm 1)

A harness `H(W, P)` is a workflow `W` (`seeds/agents/<task>/v0001/workflow.py`) plus a set of
plugins `P`, chosen in `harness.yaml` from a plugin library `L`. Each iteration runs the
stages listed in `PHASES` (`src/pluginrsi/search/loop.py`):

1. `plugin_mutation` (`search/mutation.py`): `N = offspring_per_phase` branches. Each branch
   samples a balanced minibatch of `b = feedback_batch_size` held-in tasks, and a proposer
   rewrites **one** plugin while the workflow and all other plugins stay frozen. If the
   paired gain over the incumbent is strictly positive, the variant is published to `L`.
2. `harness_recomposition` (`Search.harness_recomposition`): a proposer picks plugins from
   the updated `L` and rewrites the workflow. The candidate is evaluated on the full held-in
   set and replaces the incumbent only if its score is strictly higher.

## Where things live

| Concern | File |
| --- | --- |
| Plugin interfaces (`Role`, `Tool`, `Skill`, `Memory`) and `Task` / `SolveResult` | `src/pluginrsi/contracts.py` |
| Runtime contract shown to proposers | `src/pluginrsi/prompts/interfaces.md` |
| Plugin and harness manifests, run config (`SearchOptions`, `EvaluationConfig`) | `src/pluginrsi/schemas.py` |
| Loading and validating harnesses | `src/pluginrsi/loader.py` |
| Optimization loop, evaluation budget, proposals, held-out report | `src/pluginrsi/search/loop.py` |
| Plugin mutation stage | `src/pluginrsi/search/mutation.py` |
| Stage instructions given to the proposer | `src/pluginrsi/prompts/<stage>.md`, `fitness_contract.md` |
| Proposer agent (tools, workspace, evidence) | `search/api_proposer.py`, `text_proposer.py`, `structured_proposer.py`, `json_proposer.py` |
| Proposal validation rules per stage | `search/proposer.py::validate_proposal` |
| Benchmark workers | `src/pluginrsi/evaluation/worker.py`, `evaluation/benchmarks/*.py` |
| Evaluation scheduling, retries, quality threshold | `src/pluginrsi/evaluation/runner.py` |

## Recipes

### New plugin
1. Create `seeds/plugin_library/<kind>/<name>/v0001/` containing `__init__.py`, `plugin.yaml`
   and `implementation.py`. Copy an existing plugin of the same kind as a template (for
   example `skill/debugging/v0001`).
2. Fill `plugin.yaml`: `schema_version: 1`, `kind`, `name`, `version`,
   `entrypoint: implementation:<Class>`, `description`, `config_schema`, and `provenance`
   (`label`, `sources`, `upstream_assets`, `adaptation`).
3. Subclass the matching base class in `contracts.py`. Use relative imports only, never
   import another plugin, and avoid `hashlib` (proposal validation rejects it). Files the
   plugin reads go next to it and are accessed via `self.services.resource_dir`.
4. Add the plugin to a harness under an alias in `harness.yaml`, then run
   `pluginrsi validate --harness <harness_dir> --library seeds/plugin_library`.

### New seed harness or task
Copy `seeds/agents/<task>/v0001/`. The workflow class needs
`async run(task, runtime, plugins) -> SolveResult`; it decides when each plugin alias is
called. Point `seed:` in the config at the new directory.

### New benchmark
1. Add the name to `EvaluationConfig.benchmark` in `schemas.py`.
2. Implement `async evaluate_<name>(request) -> dict` in `evaluation/benchmarks/<name>.py`.
   It must run `loader.execute(...)` on the candidate, write the trajectory to
   `work_dir/trajectory.jsonl`, and return `{"score": float, "status": ...}`. `status` is
   one of `completed`, `solver_limit`, `infra_error` or `candidate_error`. `infra_error`
   tasks are excluded from accuracy and retried; any `candidate_error` invalidates the
   whole evaluation.
3. Dispatch to it in `evaluation/worker.py`. Add any data preparation under `scripts/`,
   ship the split ID files under `datasets/<name>/`, and add a config in `configs/`.
4. `qa_transfer.py` is the simplest complete example; Harbor-based tasks use `terminal_bench.py`.

### Change the algorithm
- **New stage:** append its name to `PHASES`, register a coroutine for it in
  `Search.phase()`, add `prompts/<stage>.md`, and teach `validate_proposal` and
  `api_proposer.py` which files that stage may write. The stage count is derived from
  `PHASES` everywhere else.
- **Selection or retention rule:** mutation retention is `mutation.paired`; incumbent
  replacement is `Search.admit` plus `selection.top_w`.
- **Minibatch sampling:** `mutation.balanced_batch` and `create_branches`. Note that
  branches are assigned plugins round-robin over the incumbent's aliases.
- **Proposer behaviour:** edit the markdown files in `prompts/`. Keep `fitness_contract.md`
  truthful about how candidates are actually scored.
- Expose new hyperparameters as fields on `SearchOptions` and annotate them with the
  paper symbol they correspond to.

## Invariants (do not break)

- Held-out tasks never influence selection. They are evaluated for reporting only.
- Published library plugins are immutable. Mutated plugins get the controller-assigned
  version `v_<candidate_id>`.
- Every evaluation goes through `Search.checked_evaluate`, which reserves the held-out
  budget against `max_task_rollouts`.
- The search must be resumable. Allocate IDs before any async work, persist records with
  `write_json` after each state change, and make every stage idempotent when re-entered.
- Rank candidates only after their evaluation wave finishes, so that completion order
  cannot decide ties.

## Testing without APIs or Docker

Tests run fully offline. `tests/test_full_search.py` provides `config_at` (benchmark
`fixture`), a deterministic `Evaluator` and a file-writing `Proposer`. Reuse them for new
search logic and assert on `runs/.../iterations/iter_*/<stage>.json`. Run
`python3 -m pytest -q` before finishing. QA tests additionally need
`scripts/prepare_hf_data.py qa_transfer` to have been run.
