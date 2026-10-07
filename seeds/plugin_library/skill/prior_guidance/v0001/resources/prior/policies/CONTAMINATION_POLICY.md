# Benchmark and Trajectory Contamination Policy

## Separation

- Use SWE-bench development/training partitions, synthetic tasks, or unrelated public repository episodes for prompt/skill development.
- Keep final held-out benchmark tasks, gold patches, hidden tests, evaluator-only metadata, and task-specific demonstrations outside all runtime-visible libraries.
- Namespace every trajectory and memory record by dataset, split, repository and task family.

## Allowed extraction from trajectories

Allowed:

- generic action sequences;
- recurring failure signatures;
- tool-use anti-patterns;
- verification and stop conditions;
- context-selection rules;
- component-level credit with task identifiers removed.

Not allowed:

- exact patch hunks;
- issue-specific file or symbol shortcuts;
- hidden-test names or outputs;
- gold localization;
- memorized repository-task pair answers;
- evaluator state that is unavailable to the solver.

## Promotion gate

A trajectory-derived lesson may enter procedural memory only after de-identification and counterexample review. It may enter the skill library only after matched-budget evaluation on disjoint positive- and negative-trigger tasks.

## Audit fields

Every derived asset must retain source dataset, split, task IDs in protected provenance, extraction version, de-identification status, evaluator version and promotion decision.
