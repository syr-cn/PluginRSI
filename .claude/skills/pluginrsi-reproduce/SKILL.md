---
name: pluginrsi-reproduce
description: Reproduce the PluginRSI paper experiments (SWE-bench Verified, Terminal-Bench 2.1, QA transfer) end to end - install, download data, fill model/sandbox settings, launch, monitor, resume and collect held-out results. Use when the user wants to run, reproduce, resume or inspect a PluginRSI experiment.
---

# Reproduce PluginRSI experiments

Drive the user through one experiment at a time. Every step below has full details in
[reference.md](reference.md); read the matching section before acting on it.

## Ask first

Confirm these with the user before changing files or launching anything:

1. **Benchmark**: `swe` (SWE-bench Verified), `terminal_bench` (Terminal-Bench 2.1) or `qa_transfer`.
2. **Models**: exact solver and proposer (evolver) model IDs, and OpenAI-compatible base URLs
   with tool calling. The paper used xhigh reasoning for the proposer and none for the solver.
3. **Sandbox** (SWE / Terminal only): Docker, or another Harbor environment they already use.
4. **Budget**: each full run uses up to 24,000 solver rollouts plus proposer calls. Make sure
   the user accepts this cost before running `start`.

Never invent API keys, endpoints or model names. Credentials go in `.env`, never in YAML or commits.

## Procedure

| Step | Action | Reference |
| --- | --- | --- |
| 1 | Create `.venv` (Python 3.11+), `pip install -e '.[test,data,qa,harbor]'`, `cp .env.example .env` | §1 |
| 2 | `python3 scripts/prepare_hf_data.py <benchmark> 2>&1 \| tee logs/prepare_<benchmark>.log` | §2 |
| 3 | Fill `.env`, then `solver.model`, `proposer.agent.model` and the sandbox block in `configs/<benchmark>.yaml`. QA also needs `datasets/qa_transfer_v1/judge.yaml` and `QA_JUDGE_*` | §3 |
| 4 | Offline checks: `pluginrsi validate ...` for the seed, then `python3 -m pytest -q` | §4 |
| 5 | `bash scripts/launch_experiment.sh check --config configs/<benchmark>.yaml` (no model or sandbox calls) | §4 |
| 6 | After the user approves the cost: `bash scripts/launch_experiment.sh start --config configs/<benchmark>.yaml` | §4 |
| 7 | Monitor `runs/<benchmark>/<run-id>/logs/run.log`; on interruption use `launch_experiment.sh resume --run ...` | §5 |
| 8 | Report from `run/heldout_report.json` and `run/state.json` | §5 |

The SWE config also needs `evaluation.benchmark_options.command_prelude` set as described
in §3, otherwise solver commands run outside the task's conda environment.

## Rules that keep results comparable

- Keep the default dataset revisions, split IDs (`datasets/*/heldin*.jsonl`, `heldout*.jsonl`),
  random seed 42 and search hyperparameters unless the user explicitly wants an ablation.
- Held-out scores are reporting-only. Never feed held-out tasks or QA reference answers
  to the solver or proposer.
- Do not edit `runs/<...>/code_snapshot` or the configuration of an active run. Resume only
  after the previous controller has stopped.
- When reporting, include valid-task counts, timeouts, exact model versions and any config
  overrides.

## Troubleshooting

- `check` fails with "Fill solver.model": step 3 is incomplete.
- `Missing task directory`: rerun data preparation; move existing Terminal task directories aside first.
- Proxy needed: set `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in the shell, not in the configs.
- Many `infra_error` results: lower `evaluation.concurrency` or the RPM limits (§3, resource settings).
