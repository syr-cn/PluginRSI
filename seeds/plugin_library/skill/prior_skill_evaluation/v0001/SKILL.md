---
name: skill-evaluation
description: Evaluate a candidate skill or skill edit on positive and negative triggers under a matched budget.
---

# Skill Evaluation

**Internal ID:** `S36`  
**Category:** `skill-evolution`  
**Tier:** `meta`  
**Output:** `SkillEvaluationRecord`

## Activate when

- The workflow reaches the `skill-evolution` stage and the required inputs are present.

## Applicable roles

- Judge
- Verifier
- Skill Compiler

## Required inputs

- `candidate_skill`
- `baseline_skill`
- `evaluation_tasks`
- `budget`

## Optional inputs

- `historical_results`

## Procedure

1. Separate positive-trigger, negative-trigger, transfer, and regression tasks.
2. Run baseline and candidate with identical model, scaffold, evaluator, and budget.
3. Measure task success, activation precision, cost, invalid actions, and regressions.
4. Promote only repeatable gains and preserve failed versions as provenance.

## Verification

- Budgets and task sets are matched.
- Activation behavior is measured, not only final reward.

## Stop conditions

- Promote, revise, reject, or keep provisional with evidence.

## Constraints

- Do not tune and evaluate on the same held-out set.
- Do not promote from one anecdotal win.

## Allowed tools

- `shell_exec`
- `run_test`
- `read_file`

## Provenance

Synthesized from design patterns in `anthropic-skills`, `openhands-extensions`, `swe-smith`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.