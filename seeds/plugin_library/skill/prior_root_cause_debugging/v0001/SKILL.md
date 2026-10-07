---
name: root-cause-debugging
description: Infer and test the smallest causal explanation instead of stacking speculative fixes.
---

# Root-cause Debugging

**Internal ID:** `S13`  
**Category:** `diagnosis`  
**Tier:** `core`  
**Output:** `RootCauseDiagnosis`

## Activate when

- The workflow reaches the `diagnosis` stage and the required inputs are present.

## Applicable roles

- Critic
- Solver

## Required inputs

- `issue_profile`
- `reproduction_result`
- `localized_code`

## Optional inputs

- `trajectory`
- `memory_hits`

## Procedure

1. Separate symptom, trigger, faulty state or assumption, and violated contract.
2. Form two or more competing hypotheses when uncertainty is material.
3. Choose the cheapest discriminating check.
4. Update or reject hypotheses from fresh evidence.
5. Stop when one mechanism explains the failure and predicts the fix.

## Verification

- The chosen cause explains both observed and expected behavior.
- Rejected hypotheses retain their evidence.

## Stop conditions

- One falsifiable root cause is supported or diagnosis is explicitly inconclusive.

## Constraints

- Do not apply multiple speculative fixes at once.
- Do not label the first suspicious line as root cause.

## Allowed tools

- `read_range`
- `shell_exec`
- `inspect_failure`
- `text_search`

## Provenance

Synthesized from design patterns in `superpowers`, `addy-agent-skills`, `swe-agent`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.