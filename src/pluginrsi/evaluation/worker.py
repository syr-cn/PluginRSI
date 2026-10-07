import asyncio
import sys
import traceback
from pathlib import Path

from ..search.store import read_json, write_json
from ..tracing import origin
from .benchmarks.terminal_bench import evaluate_harbor


async def main(path: Path):
    request = read_json(path)
    if request['evaluation'].get('worker_initialization_timeout_seconds') is not None:
        from .progress import milestone
        milestone(Path(request['work_dir']), 'worker_main')
    try:
        if request["evaluation"]["benchmark"] == "fixture":
            raise ValueError("fixture benchmarks are available only through injected test evaluators")
        from .dependency_versions import prepare_dependency_versions
        versions = prepare_dependency_versions()
        write_json(Path(request['work_dir']) / 'dependency_versions.json', versions)
        if request['evaluation'].get('worker_initialization_timeout_seconds') is not None:
            milestone(Path(request['work_dir']), 'dependency_versions_ready')
        with origin(request.get("accounting", {})):
            if request["evaluation"]["benchmark"] == 'qa_transfer':
                from .benchmarks.qa_transfer import evaluate_qa
                result = await evaluate_qa(request)
            else:
                result = await evaluate_harbor(request)
    except Exception as error:
        traceback.print_exc()
        result = {"score": 0.0, "status": "infra_error", "detail": repr(error)}
    write_json(Path(request["work_dir"]) / "result.json", result)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
