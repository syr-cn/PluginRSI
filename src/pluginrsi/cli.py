import argparse
import asyncio
import fcntl
import json
from pathlib import Path

from dotenv import load_dotenv

from .evaluation.runner import Evaluator, read_tasks
from .loader import library_refs, resolve_plugin, validate
from .schemas import load_config
from .search.loop import Search, initialize
from .search.selection import is_valid
from .search.store import Store


async def dispatch(args):
    if args.command == "catalog":
        libraries = [path.resolve() for path in args.library]
        catalog = []
        for ref in library_refs(libraries):
            _, manifest = resolve_plugin(ref, libraries)
            catalog.append({"ref": ref, "description": manifest.description, "provenance": manifest.provenance})
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
        return
    if args.command == "validate":
        workflow, plugins = validate(args.harness.resolve(), [p.resolve() for p in args.library])
        print(json.dumps({"workflow": workflow.__name__, "plugin_aliases": list(plugins)}))
        return
    if args.command in ("resume", "report-heldout"):
        config = load_config(args.run / "config.yaml")
    else:
        config = load_config(args.config)
        if args.command == "evaluate":
            config.seed = args.harness.resolve()
    config.run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.run_dir / ".controller.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(config.run_dir) if args.command in ("resume", "report-heldout") else initialize(config)
        if args.command == "report-heldout":
            evaluator = Evaluator(config, store, retry_infra=getattr(args, "retry_infra", False))
            report = await Search(config, store, evaluator=evaluator).report_heldout()
            print(json.dumps(report, indent=2))
            return 0 if all(item["valid"] for item in report["results"].values()) else 2
        elif args.command == "evaluate":
            tasks_path = getattr(config.evaluation, args.split + "_tasks")
            if tasks_path is None:
                raise ValueError(f"no dataset configured for {args.split}")
            result = await Evaluator(config, store).evaluate("c000000", read_tasks(tasks_path), args.split, "manual")
            print(json.dumps({key: result[key] for key in ("id", "status", "mean_score")}, indent=2))
            return 0 if is_valid(result) else 2
        else:
            evaluator = Evaluator(config, store, retry_infra=getattr(args, "retry_infra", False))
            best = await Search(config, store, evaluator=evaluator).run()
            print(json.dumps({"best": best, "stop_reason": store.state["stop_reason"], "run": str(store.root)}, indent=2))
            return 2 if best is None or store.state["stop_reason"] in ("invalid_parent_evaluation", "proposal_infra_error") else 0


def main():
    parser = argparse.ArgumentParser(prog="pluginrsi")
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    commands = parser.add_subparsers(dest="command", required=True)
    validation = commands.add_parser("validate")
    validation.add_argument("--harness", type=Path, required=True)
    validation.add_argument("--library", type=Path, action="append", required=True)
    catalog = commands.add_parser("catalog")
    catalog.add_argument("--library", type=Path, action="append", required=True)
    for name in ("search", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "evaluate":
            command.add_argument("--harness", type=Path, required=True)
            command.add_argument("--split", choices=["feedback", "selection", "heldout"], default="selection")
    resume = commands.add_parser("resume")
    resume.add_argument("--run", type=Path, required=True)
    resume.add_argument("--retry-infra", action="store_true")
    report = commands.add_parser("report-heldout")
    report.add_argument("--run", type=Path, required=True)
    report.add_argument("--retry-infra", action="store_true")
    args = parser.parse_args()
    for path in args.env_file:
        load_dotenv(path, override=False)
    load_dotenv(override=False)
    raise SystemExit(asyncio.run(dispatch(args)))


if __name__ == "__main__":
    main()
