---
name: hypothesis-testing
description: Design cheap discriminating checks between competing failure hypotheses.
---

# Hypothesis Testing

**Internal ID:** `S14`  
**Category:** `diagnosis`  
**Tier:** `optional`  
**Output:** `HypothesisTestPlan`

## Activate when

- The workflow reaches the `diagnosis` stage and the required inputs are present.

## Applicable roles

- Critic
- Solver
- Planner

## Required inputs

- `hypotheses`
- `available_evidence`
- `tool_budget`

## Procedure

1. For each hypothesis, state a prediction that differs from alternatives.
2. Choose the lowest-cost safe observation, command, or temporary instrumentation.
3. Run one check at a time and update posterior confidence.
4. Remove temporary instrumentation after use.

## Verification

- Each check can change the ranking of hypotheses.
- Results are recorded with commands and provenance.

## Stop conditions

- One hypothesis dominates or the remaining uncertainty is irreducible under budget.

## Constraints

- Do not make production edits as diagnostic instrumentation.
- Do not retain debug prints.

## Allowed tools

- `shell_exec`
- `read_range`
- `inspect_failure`

## Provenance

Synthesized from design patterns in `superpowers`, `addy-agent-skills`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.