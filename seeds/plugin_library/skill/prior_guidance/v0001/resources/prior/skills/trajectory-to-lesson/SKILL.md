---
name: trajectory-to-lesson
description: Extract a bounded transferable procedure or anti-pattern from a completed trajectory.
---

# Trajectory to Lesson

**Internal ID:** `S31`  
**Category:** `learning`  
**Tier:** `meta`  
**Output:** `LessonCandidate`

## Activate when

- The workflow reaches the `learning` stage and the required inputs are present.

## Applicable roles

- Reflection Agent
- Memory Curator

## Required inputs

- `trajectory`
- `outcome`
- `task_profile`

## Optional inputs

- `component_credit`
- `counterfactuals`

## Procedure

1. Locate a decision or procedure with plausible causal impact.
2. Remove task IDs, repository names, exact patches, hidden-test hints, and incidental details.
3. Write trigger, procedure or anti-pattern, scope, evidence, counterexample, and confidence.
4. Reject lessons that are merely summaries or one-off preferences.

## Verification

- The lesson can apply to a different repository/task.
- Evidence and counterexample are preserved.

## Stop conditions

- One bounded lesson is proposed or the trajectory is rejected for learning.

## Constraints

- No benchmark answer memorization.
- Do not infer causality from sequence alone.

## Allowed tools

- `read_file`

## Provenance

Synthesized from design patterns in `swe-bench-experiments`, `swe-smith-trajectories`, `openhands-extensions`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.