# PluginRSI reproduction reference

Run all commands from the repository root on Linux. The Python package and CLI are
both named `pluginrsi`.

## 1. Install

Use Python 3.11+ (3.12 recommended), `git`, `rsync`, and `tmux`. SWE-bench and Terminal Bench additionally need
a working container backend; the instructions below use Docker with Harbor.
Install Docker and verify that your user can run containers before starting.
QA transfer runs locally and does not require Docker.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[test,data,qa,harbor]'
mkdir -p logs
set -o pipefail
cp .env.example .env
```

If needed, set your own `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` in the shell.
No proxy, private API, cluster ID, or credentials are provided. Public datasets
normally require no token; set `HF_TOKEN` in the shell or `.env` for an authenticated Hugging Face session.
Keep `.env` local.

## 2. Download and prepare datasets

The repository includes the original ordered split IDs and the QA data schema.
The following commands download data from Hugging Face and prepare evaluator inputs;
they do not call a model or launch a sandbox.

```bash
python3 scripts/prepare_hf_data.py swe 2>&1 | tee logs/prepare_swe.log
python3 scripts/prepare_hf_data.py terminal_bench 2>&1 | tee logs/prepare_terminal.log
python3 scripts/prepare_hf_data.py qa_transfer 2>&1 | tee logs/prepare_qa.log
```

Only prepare the benchmark you plan to run. Downloads are cached in `data_raw/`;
use `--cache /absolute/cache/path` to put them elsewhere. Dataset repositories and
immutable revisions are defined at the top of `scripts/prepare_hf_data.py`.
The generated `provenance.json` records source versions. To change a SWE or Terminal
source, pass `--repo` and `--revision`; QA accepts `--revisions path/to/revisions.json`
(a JSON mapping of dataset repository IDs to commit hashes). Keep default revisions
and split IDs when reproducing these configurations.

| Experiment | Hugging Face source | Held-in / held-out | Generated input |
| --- | --- | --- | --- |
| SWE-bench Verified | `SWE-bench/SWE-bench_Verified` | 400 / 100 | `datasets/swe/tasks/`, manifests with absolute task paths |
| Terminal Bench 2.1 | `harborframework/terminal-bench-2.1` | 59 / 30 | `datasets/terminal_bench_2_1/tasks/`, manifests with absolute task paths |
| QA transfer | Sources below | 150 / 250 | `datasets/qa_transfer_v1/data.jsonl` and ID manifests |

### SWE-bench

The builder uses the dataset's problem statement, public container image and
evaluation script to create Harbor tasks. It excludes the solution patch from
agent instructions. Images are pulled when the sandbox starts; downloading the
dataset does not download the container images.

The verifier preserves this experiment's grading rule: reward 1 when the test
process exits successfully, otherwise 0. It captures the exit status before
logging markers and Git cleanup. This is the original adapter's rule, not the
official SWE-bench per-test `FAIL_TO_PASS` / `PASS_TO_PASS` log-parser metric.
Do not compare the two metrics as if they were identical.

### Terminal Bench

The builder copies task instructions, environment definitions and official
verifiers from the pinned HF task snapshot, selecting the original 59/30 split.
It refuses to overwrite an existing task directory. To rebuild, move the generated
`datasets/terminal_bench_2_1/tasks/` directory aside and rerun the command.

Terminal Bench runs through Harbor with the same workflow/plugin interfaces as
SWE-bench. Use the same model, resources and task versions when comparing reruns.

### QA transfer

The builder downloads the following raw files from HF, including SciCite's
converted training Parquet. It does not execute dataset loading scripts.

| Dataset | Held-in | Held-out |
| --- | --- | --- |
| `Hothan/OlympiadBench` | 50 English text-only math | 50 English text-only physics |
| `m-a-p/SuperGPQA` | 50 Economics | 50 Medicine + 50 Law |
| `allenai/scicite` | 50 citation-intent classification | — |
| `openai/frontierscience` | — | 50 olympiad |
| `AfterQuery/FinanceQA` | — | 50 finance |

`scripts/prepare_qa_transfer.py` implements the original seed-42 sampling,
stratification, quotas, deduplication and schema checks. The HF entry point calls
that implementation and requires the resulting ordered IDs to match the shipped
cohort. It reconstructs SciCite's original `unique_id` from `id` and `excerpt_index`.
Upstream changes that alter the cohort produce an error instead of silently
changing the benchmark. Existing different QA artifacts are never overwritten.

To validate prepared QA data without downloading again:

```bash
python3 scripts/prepare_qa_transfer.py --validate-only 2>&1 | tee logs/validate_qa.log
```

QA answer data belongs to the evaluator. Do not add `data.jsonl` or reference
answers to solver/proposer prompts. Math uses the bundled OlympiadBench grader;
choice/classification use exact matching; FinanceQA and FrontierScience use the
configured reference judge. Preserve source dataset licenses and attribution when
redistributing downloaded assets; the source repositories carry their data cards.

## 3. Fill model and sandbox settings

There are exactly three main experiment definitions:
`configs/swe.yaml`, `configs/terminal_bench.yaml`, and `configs/qa_transfer.yaml`.
Each runs the full method: plugin mutation followed by harness recomposition.

Fill these values before running:

| Location | Required values |
| --- | --- |
| `.env` | `SOLVER_API_KEY`, `SOLVER_BASE_URL`, `EVOLVER_API_KEY`, `EVOLVER_BASE_URL` |
| Experiment YAML `solver.model` | Your solver's exact model identifier |
| Experiment YAML `proposer.agent.model` | Your evolver's exact model identifier |
| SWE / Terminal `evaluation.benchmark_options.environment.type` | `docker`, or a Harbor-supported backend you configure |
| QA `datasets/qa_transfer_v1/judge.yaml` | Set `model` and provider-compatible API options |
| `.env`, for QA | `QA_JUDGE_API_KEY`, `QA_JUDGE_BASE_URL` |

API URLs must be OpenAI-compatible base URLs, including `/v1` when required by
your provider. The templates use `chat_completions`; `responses` is also supported.
Both solver and evolver need tool calling. Adjust `reasoning_effort`,
`thinking_mode`, and JSON-mode settings to match your endpoint. The QA judge's
default `chat_json_mode: native` requires JSON response-format support; use
`prompt` for providers without it. Model identifiers and access details are
intentionally blank: use the exact intended model version for a comparable result.

For Docker, the sandbox portion of each SWE / Terminal configuration becomes:

```yaml
benchmark_options:
  worker_python: ''
  module_paths: []
  environment:
    type: docker
```

An empty `worker_python` uses the launcher's interpreter. For a separate worker
environment, set its absolute Python path and install this package, Harbor and the
benchmark dependencies there too. `module_paths` is only needed for a custom
adapter. To use a custom Harbor environment, replace `type` with an `import_path`
and provide that adapter's `kwargs`; supply credentials through its own environment
variables. No private sandbox is selected by default.

For the SWE images above, also set `evaluation.benchmark_options.command_prelude`
to `source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && export PLUGINRSI_HELPER_PYTHON=/opt/miniconda3/bin/python3 && cd /testbed`.
This activates the task environment before every solver shell command. Leave it
empty for Terminal Bench, whose tasks define their own container environments.

### Search and resource settings

| Parameter | SWE | Terminal Bench | QA transfer |
| --- | ---: | ---: | ---: |
| Search iterations `T` (`iterations`) | 15 | 15 | 10 |
| Mutation branches `N` (`offspring_per_phase`) | 8 | 8 | 8 |
| Minibatch size `b` (`feedback_batch_size`) | 20 | 20 | 20 |
| Failed tasks per minibatch (`local_failure_tasks`) | 10 | 10 | 10 |
| Recomposition candidates (`recomposition_offspring`) | 1 | 1 | 1 |
| Proposer reasoning effort | xhigh | xhigh | xhigh |
| Global evaluation workers | 128 | 128 | 128 |
| Solver max calls per task | 30 | 100 | 20 |
| Min. valid full held-in results | 390 / 400 | 55 / 59 | 145 / 150 |
| Rollout budget | 24,000 | 24,000 | 24,000 |

Feedback and selection use the same ordered held-in cohort. Held-out scores do
not select candidates. A full evaluation counts only if at least the number of
tasks above return valid results (infrastructure failures are excluded, candidate
errors invalidate it). Minibatches use `min_valid_ratio`; full evaluations use
`full_min_valid_ratio`. Held-out evaluation runs in parallel with held-in
evaluation and is reported only.

Solver and evolver share a 300 RPM allowance **within each experiment**, using
that experiment's `.rate_limits/<experiment>.json`. Different experiments have
separate state files. Retries are bounded and adaptive RPM reduction is disabled.
If you run multiple replicas of one configuration, give each a distinct experiment
name and rate-limit state path. Adjust concurrency/RPM for your hardware and API
quota; changes can affect scheduling and should be recorded with reported results.

## 4. Check and start

First validate the seed and run the regression suite. The full suite needs the QA
dataset prepared in step 2; it uses fixtures instead of real model requests.

```bash
pluginrsi validate --harness seeds/agents/swe_bench_verified/v0001 \
  --library seeds/plugin_library 2>&1 | tee logs/validate_swe_seed.log
pluginrsi validate --harness seeds/agents/terminal_bench_2_1/v0001 \
  --library seeds/plugin_library 2>&1 | tee logs/validate_terminal_seed.log
pluginrsi validate --harness seeds/agents/qa_transfer/v0001 \
  --library seeds/plugin_library_qa 2>&1 | tee logs/validate_qa_seed.log
python3 -m pytest -q 2>&1 | tee logs/tests.log
```

Choose one configuration; substitute the other main config paths to run those
experiments. The wrapper automatically records launcher output in `logs/launcher/`.

```bash
bash scripts/launch_experiment.sh check --config configs/swe.yaml
bash scripts/launch_experiment.sh start --config configs/swe.yaml
```

`check` validates filled settings, dataset counts, disjoint splits, available task
directories, seed loading and budget consistency. It makes no model or sandbox
requests, so it does not prove API connectivity or successful container startup.
`start` performs the same checks, freezes code and data paths, creates a run and
launches services in tmux. It requires a populated `.env` file and `tmux` on PATH.
The launcher works with either a Git checkout or an unpacked source archive.

To prepare an inspectable run before launching:

```bash
bash scripts/launch_experiment.sh prepare --config configs/swe.yaml --run-id reproduction
bash scripts/launch_experiment.sh start --run runs/swe/reproduction
```

The launcher uses `.venv/bin/python3` by default. Set `PLUGINRSI_PYTHON` to another
Python 3 interpreter if necessary. Run paths are resolved relative to the project.
The prepared task manifests contain absolute paths: after moving the checkout,
rerun data preparation to update them (move Terminal task directories aside first).

## 5. Monitor, resume and collect results

Launcher output prints the persistent run directory and tmux session. Runs have
separate persistent metadata (`runs/<experiment>/<run-id>/`) and working data
(`.work/<experiment>/<run-id>/`). Preserve both directories, or the runtime backup,
when archiving a run. Do not edit frozen code/configuration in an active run.

```bash
tmux list-sessions
tail -f runs/swe/<run-id>/logs/run.log
bash scripts/launch_experiment.sh resume --run runs/swe/<run-id>
```

Resume only after the previous controller has stopped; the launcher prevents two
controllers from using the same run. Close any stopped tmux session named in its
error before resuming. If local working data was lost, restore `runtime_backup/`
to the exact `.work/<experiment>/<run-id>/` path recorded in `launch.json` first.
Resuming retries infrastructure failures within the configured budgets.

Key outputs under `runs/<experiment>/<run-id>/`:

| Path | Contents |
| --- | --- |
| `source-config.yaml`, `config.yaml`, `plan.json` | Submitted settings, resolved configuration, rollout plan |
| `runtime_snapshot.json`, `code_snapshot/` | Source revision (when available) and frozen runtime code |
| `run/state.json` | Search state, selected candidates, stop reason |
| `run/evaluations/` | Per-task results, trajectories, immutable selection decisions and live scores |
| `run/candidates/`, `run/plugin_library/` | Candidate harnesses and evolved plugins |
| `run/heldout_report.json` | Seed/final held-out comparison after search |
| `logs/`, `api_solver.jsonl`, `api_evolver.jsonl` | Service logs and API accounting |

The supervisor produces the final held-out report after completed search (or an
allowed budget stop). A threshold-released score is provisional; use completed
evaluation records and report valid-task counts, timeouts, model versions,
dataset revisions and any configuration overrides when reporting a reproduction.
