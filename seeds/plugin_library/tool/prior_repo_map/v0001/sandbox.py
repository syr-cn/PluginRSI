import ast
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

PAYLOAD = json.loads(sys.argv[1])
ARGS = PAYLOAD["arguments"]
CONFIG = PAYLOAD["config"]
ROOT = Path(CONFIG.get("repository_root", ".")).resolve()
LIMIT = CONFIG.get("max_output_bytes", 32768)
TIMEOUT = min(ARGS.get("timeout_seconds", CONFIG.get("timeout_seconds", 180)),
              CONFIG.get("timeout_seconds", 180))


def scoped(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"path outside repository: {value}")
    return path


def run(command, cwd=ROOT, shell=False, env=None):
    start = time.monotonic()
    # The containing environment enforces the outer timeout and sandbox boundary.
    with subprocess.Popen(command, cwd=cwd, shell=shell, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            timed_out = True
    return {"exit_code": process.returncode, "stdout": stdout[:LIMIT].decode(errors="replace"),
            "stderr": stderr[:LIMIT].decode(errors="replace"), "duration_seconds": time.monotonic() - start,
            "truncated": len(stdout) > LIMIT or len(stderr) > LIMIT, "timed_out": timed_out}


def git(*args, cwd=ROOT):
    return run(["git", *args], cwd=cwd)


def files(roots, max_depth=None, hidden=False):
    for value in roots:
        base = scoped(value)
        if base.is_file():
            yield base
            continue
        for directory, dirs, names in os.walk(base):
            depth = len(Path(directory).relative_to(base).parts)
            dirs[:] = sorted(name for name in dirs if name != ".git" and (hidden or not name.startswith("."))
                             and not (Path(directory) / name).is_symlink()
                             and git("check-ignore", "-q", str(Path(directory) / name))["exit_code"] != 0)
            if max_depth is not None and depth >= max_depth:
                dirs[:] = []
                continue
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or (not hidden and name.startswith(".")):
                    continue
                if git("check-ignore", "-q", str(path))["exit_code"] != 0:
                    yield path


def read(path, maximum=1048576):
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if b"\x00" in data:
        raise ValueError("binary file rejected")
    if len(data) > maximum:
        raise ValueError("oversized file rejected; request a bounded range")
    return data.decode(ARGS.get("encoding", "utf-8"))


def search(query, roots, maximum, regex):
    pattern = re.compile(query if regex else re.escape(query))
    matches = []
    for path in files(roots):
        if path.stat().st_size > CONFIG.get("max_file_bytes", 1048576):
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            continue
        for number, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if pattern.search(line):
                if len(matches) >= maximum:
                    return matches, True
                matches.append({"path": str(path.relative_to(ROOT)), "line": number, "context": line[:1000]})
    return matches, False


def dispatch():
    name = PAYLOAD["name"]
    if name in {"shell_exec", "run_test", "run_lint"}:
        env = dict(os.environ, **ARGS.get("env_overrides", {}))
        cwd = scoped(ARGS["cwd"])
        result = run(ARGS["command"], cwd, shell=True, env=env)
        if name != "shell_exec":
            result["status"] = "timeout" if result["timed_out"] else "pass" if result["exit_code"] == 0 else "fail"
            result["command"] = ARGS["command"]
            result["revision"] = git("rev-parse", "HEAD", cwd=cwd)["stdout"].strip()
            result["diagnostics"] = result["stderr"].splitlines()
        return result
    if name == "list_tree":
        paths = files([ARGS["root"]], ARGS["max_depth"], ARGS.get("include_hidden", False))
        entries = []
        for path in paths:
            if len(entries) == ARGS["max_entries"]:
                return {"entries": entries, "truncated": True}
            entries.append(str(path.relative_to(ROOT)))
        return {"entries": entries, "truncated": False}
    if name == "read_file":
        return {"content": read(scoped(ARGS["path"]), min(ARGS["max_bytes"], CONFIG.get("max_file_bytes", 1048576))),
                "truncated": False}
    if name == "read_range":
        if ARGS["end_line"] < ARGS["start_line"] or ARGS["end_line"] - ARGS["start_line"] >= CONFIG.get("max_lines", 500):
            raise ValueError("invalid or oversized line interval")
        lines = []
        with scoped(ARGS["path"]).open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if number > ARGS["end_line"]:
                    break
                if number >= ARGS["start_line"]:
                    if "\x00" in line:
                        raise ValueError("binary file rejected")
                    lines.append(f"{number}: {line.rstrip()}")
                    if sum(len(value) for value in lines) > LIMIT:
                        raise ValueError("line range exceeds output limit")
        return {"content": "\n".join(lines), "actual_start": ARGS["start_line"] if lines else None,
                "actual_end": ARGS["start_line"] + len(lines) - 1 if lines else None}
    if name in {"text_search", "symbol_search"}:
        query = ARGS.get("query", ARGS.get("symbol_or_query"))
        matches, truncated = search(query, ARGS.get("paths", ["."]), ARGS["max_results"], ARGS.get("regex", False))
        if name == "text_search":
            return {"matches": matches, "truncated": truncated}
        definitions = [m for m in matches if re.search(r"\b(def|class|function|interface|struct)\s+" + re.escape(query) + r"\b", m["context"])]
        return {"symbols": definitions if ARGS["kind"] != "reference" else [],
                "references": matches if ARGS["kind"] != "definition" else [],
                "confidence": 0.25, "approximate": True, "truncated": truncated,
                "method": "text fallback; references may include definitions"}
    if name == "repo_map":
        budget = ARGS["token_budget"]
        lines, symbols = [], []
        for path in files([ARGS["repository_root"]]):
            if path.suffix != ".py" or path.stat().st_size > CONFIG.get("max_file_bytes", 1048576):
                continue
            source = path.read_text(errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.append({"path": str(path.relative_to(ROOT)), "name": node.name, "line": node.lineno,
                                    "kind": type(node).__name__})
        query = ARGS.get("query", "").lower()
        symbols.sort(key=lambda item: (query not in item["name"].lower(), item["path"], item["line"]))
        chosen = []
        for symbol in symbols:
            line = f"{symbol['path']}:{symbol['line']} {symbol['kind']} {symbol['name']}"
            if len(("\n".join(lines + [line])).encode()) > budget:
                break
            lines.append(line)
            chosen.append(symbol)
        return {"map": "\n".join(lines), "ranked_symbols": chosen,
                "token_estimate": len("\n".join(lines).encode()),
                "method": "Python AST structure; conservative one UTF-8 byte per token budget"}
    if name == "apply_patch":
        patch = ARGS["patch"]
        if isinstance(patch, list):
            chunks = []
            seen = set()
            for operation in patch:
                if not isinstance(operation, dict) or set(operation) != {"path", "old", "new"}:
                    raise ValueError("typed edits require exactly path, old and new string fields")
                if not all(isinstance(value, str) for value in operation.values()):
                    raise ValueError("typed edit fields must be strings")
                path = operation["path"]
                if path in seen:
                    raise ValueError("combine multiple edits of one file into one operation")
                seen.add(path)
                original = read(scoped(path), CONFIG.get("max_file_bytes", 1048576))
                if not operation["old"] or original.count(operation["old"]) != 1:
                    raise ValueError("typed edit old text must match exactly once")
                updated = original.replace(operation["old"], operation["new"], 1)
                chunks.extend(difflib.unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True),
                                                   fromfile="a/" + path, tofile="b/" + path))
            patch = "".join(chunks)
        # --numstat parses quoted paths using Git's own diff parser before any edit.
        check = subprocess.run(["git", "apply", "--numstat", "-z", "-"], input=patch,
                               text=True, capture_output=True, cwd=ROOT, timeout=TIMEOUT)
        if check.returncode:
            return {"applied": False, "changed_files": [], "errors": [check.stderr], "diff": ""}
        paths = [record.split("\t", 2)[2] for record in check.stdout.split("\x00") if record]
        for path in paths:
            scoped(path)
            if git("ls-files", "--error-unmatch", "--", path)["exit_code"]:
                raise ValueError("patch must target tracked repository files")
        command = ["git", "apply", "--check", "-"]
        check = subprocess.run(command, input=patch, text=True, capture_output=True, cwd=ROOT, timeout=TIMEOUT)
        dry_run = ARGS.get("dry_run", True)
        if check.returncode == 0 and not dry_run:
            check = subprocess.run(["git", "apply", "-"], input=patch, text=True, capture_output=True,
                                   cwd=ROOT, timeout=TIMEOUT)
        return {"applied": check.returncode == 0 and not dry_run, "valid": check.returncode == 0,
                "changed_files": paths, "errors": [check.stderr] if check.stderr else [],
                "diff": git("diff", "--", *paths)["stdout"]}
    if name == "git_status":
        cwd = scoped(ARGS["repository_root"])
        result = git("status", "--porcelain=v1", "-z", cwd=cwd)
        if result["exit_code"]:
            raise ValueError(result["stderr"])
        modified, untracked, staged = [], [], []
        records = iter(result["stdout"].split("\x00"))
        for entry in records:
            if len(entry) < 3:
                continue
            status, path = entry[:2], entry[3:]
            if status == "??":
                untracked.append(path)
            else:
                if status[0] != " ":
                    staged.append(path)
                if status[1] != " ":
                    modified.append(path)
                if "R" in status or "C" in status:
                    next(records, None)
        return {"branch": git("branch", "--show-current", cwd=cwd)["stdout"].strip(),
                "modified": modified, "untracked": untracked, "staged": staged}
    if name == "git_diff":
        paths = ARGS.get("paths", [])
        for path in paths:
            scoped(path)
        options = ["--cached"] if ARGS.get("staged", False) else []
        result = git("diff", *options, "--", *paths)
        if result["exit_code"]:
            raise ValueError(result["stderr"])
        names = subprocess.run(["git", "diff", "--name-only", "-z", *options, "--", *paths],
                               cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
        diff = result["stdout"].encode()
        limit = min(ARGS["max_bytes"], LIMIT)
        return {"diff": diff[:limit].decode(errors="replace"),
                "changed_files": [p for p in names.stdout.split("\x00") if p],
                "truncated": result["truncated"] or len(diff) > limit}
    if name == "inspect_failure":
        text = ARGS["stdout"] + "\n" + ARGS["stderr"]
        frames = re.findall(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?', text)
        errors = re.findall(r"^([A-Za-z_][\w.]*(?:Error|Exception):.*)$", text, re.MULTILINE)
        return {"failure_class": errors[-1].split(":")[0] if errors else "unknown",
                "primary_error": errors[-1] if errors else "", "frames": frames,
                "candidate_symbols": [frame[2] for frame in frames if frame[2]],
                "raw_output": {"command": ARGS["command"], "stdout": ARGS["stdout"], "stderr": ARGS["stderr"]}}
    if name == "test_discovery":
        symbols = ARGS["symbols"]
        query = "|".join(re.escape(symbol) for symbol in symbols) if symbols else r"\btest\b"
        matches, truncated = search(query, [ARGS["repository_root"]], ARGS["max_results"] * 10, True)
        tests = [match for match in matches if "test" in match["path"].lower()][:ARGS["max_results"]]
        evidence = []
        for filename in ["AGENTS.md", "CONTRIBUTING.md", "Makefile", "pyproject.toml", "package.json", "pytest.ini"]:
            path = scoped(str(Path(ARGS["repository_root"]) / filename))
            if path.is_file():
                evidence.append({"path": str(path.relative_to(ROOT)), "content": read(path, CONFIG.get("max_file_bytes", 1048576))[:LIMIT]})
        return {"tests": tests, "commands": [], "evidence": evidence, "truncated": truncated,
                "method": "symbol text relevance; derive exact commands from repository evidence"}
    raise ValueError(f"unknown tool: {name}")


try:
    print(json.dumps(dispatch(), ensure_ascii=False))
except (ValueError, OSError, re.error, subprocess.TimeoutExpired) as error:
    print(json.dumps({"error": str(error)}))
    sys.exit(1)
