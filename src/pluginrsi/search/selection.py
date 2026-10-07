def top_w(archive: dict, width: int) -> list[str]:
    return sorted(archive, key=lambda cid: (-archive[cid]["mean_score"], cid))[:width]


def is_valid(result: dict) -> bool:
    rows = result["scores"]
    valid = sum(row["status"] in ("completed", "solver_limit") for row in rows)
    denominator = len(result.get("task_ids", rows)) if result["status"] == "search_ready" else len(rows)
    return (result["status"] in ("completed", "search_ready") and bool(rows)
            and valid / denominator >= result.get("min_valid_ratio", 1.0)
            and all(row["status"] in ("completed", "solver_limit", "infra_error") for row in rows))


def valid_scores(result):
    return {row["task_id"]: row["score"] for row in result["scores"]
            if row["status"] in ("completed", "solver_limit")}


def apply_quality_policy(record, threshold):
    rows = record["scores"]
    usable = [r for r in rows if r["status"] in ("completed", "solver_limit")]
    ratio = len(usable) / len(rows) if rows else 0
    candidate_error = any(r["status"] not in ("completed", "solver_limit", "infra_error") for r in rows)
    accepted = ratio >= threshold and bool(usable) and not candidate_error
    return {**record, "min_valid_ratio": threshold, "valid_task_count": len(usable),
            "excluded_task_ids": [r["task_id"] for r in rows if r["status"] not in ("completed", "solver_limit")],
            "valid_ratio": ratio, "status": "candidate_error" if candidate_error else "completed" if accepted else "infra_error",
            "mean_score": sum(r["score"] for r in usable) / len(usable) if accepted else None}
