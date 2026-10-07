"""Supervise one declared search and its final held-out report with bounded infra retries."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pluginrsi.schemas import load_config
from pluginrsi.search.loop import BUDGET_STOP_REASONS
from pluginrsi.search.store import read_json, write_json

MAX_INFRA_RESUMES = 2
INFRA_RESUME_DELAY_SECONDS = 60.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report-on-budget-stop", action="store_true")
    parser.add_argument("--max-infra-resumes", type=int, default=MAX_INFRA_RESUMES)
    parser.add_argument("--infra-resume-delay-seconds", type=float, default=INFRA_RESUME_DELAY_SECONDS)
    args = parser.parse_args()
    if args.max_infra_resumes < 0:
        parser.error('--max-infra-resumes must be nonnegative')
    if not 0 <= args.infra_resume_delay_seconds < float('inf'):
        parser.error('--infra-resume-delay-seconds must be finite and nonnegative')
    config = load_config(args.config)
    root = args.config.resolve().parent
    if args.resume:
        config = load_config(config.run_dir / "config.yaml")
    with (root / ".supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = [sys.executable, "-m", "pluginrsi.cli"]
        for path in args.env_file:
            base += ["--env-file", str(path.resolve())]
        def status(stage, **extra):
            value = dict(pid=os.getpid(), stage=stage, run=str(config.run_dir), **extra)
            write_json(root / "supervisor.json", value)
            print(json.dumps(value), flush=True)
        for attempt in range(args.max_infra_resumes + 1):
            resume = args.resume or attempt > 0
            command = ["resume", "--run", str(config.run_dir)] if resume else ["search", "--config", str(args.config.resolve())]
            if resume:
                command += ["--retry-infra"]
            status("search", attempt=attempt)
            code = subprocess.run(base + command).returncode
            if not (config.run_dir / "state.json").exists():
                status("stopped", exit_code=code, stop_reason="search_did_not_initialize")
                return 2
            state = read_json(config.run_dir / "state.json")
            if code == 0 and state.get("stop_reason") == "iterations_completed":
                break
            if (args.report_on_budget_stop and code == 0 and state.get("beam")
                    and state.get("stop_reason") in BUDGET_STOP_REASONS):
                break
            reason = state.get("stop_reason")
            proposal_infra = reason == "proposal_infra_error"
            evaluation_infra = reason in ("invalid_seed_evaluation", "invalid_parent_evaluation") and any(
                read_json(p).get("status") == "infra_error"
                for p in (config.run_dir / "evaluations").glob("*/evaluation.json"))
            if not (proposal_infra or evaluation_infra) or attempt == args.max_infra_resumes:
                status("stopped", exit_code=code, stop_reason=reason)
                return 2
            status("retrying", attempt=attempt, next_attempt=attempt + 1, stop_reason=reason,
                   retry_delay_seconds=args.infra_resume_delay_seconds)
            time.sleep(args.infra_resume_delay_seconds)
        if config.evaluation.heldout_tasks is None:
            status("completed", report=None, search_stop_reason=state.get("stop_reason"))
            return 0
        for attempt in range(args.max_infra_resumes + 1):
            status("heldout", attempt=attempt)
            command = ["report-heldout", "--run", str(config.run_dir)]
            if attempt:
                command += ["--retry-infra"]
            code = subprocess.run(base + command).returncode
            if code == 0:
                status("completed", report=str(config.run_dir / "heldout_report.json"),
                       search_stop_reason=state.get("stop_reason"))
                return 0
            report_path = config.run_dir / "heldout_report.json"
            if not report_path.exists():
                break
            report = read_json(report_path)
            invalid = [r for r in report["results"].values() if not r["valid"]]
            if not invalid or any(read_json(config.run_dir / "evaluations" / r["evaluation_id"] / "evaluation.json")["status"] != "infra_error" for r in invalid):
                break
        status("stopped", stop_reason="heldout_not_valid", exit_code=code)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
