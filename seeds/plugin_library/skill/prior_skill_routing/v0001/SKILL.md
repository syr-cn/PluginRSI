---
name: skill-routing
description: Select the smallest useful set of coding skills for the current stage, evidence state, role, and budget.
---

# Skill Routing

**Internal ID:** `S01`  
**Category:** `control`  
**Tier:** `core`  
**Output:** `SkillActivationPlan`

## Activate when

- A role has more available procedures than should be loaded at once.
- The workflow enters a new stage.

## Applicable roles

- Orchestrator
- Planner
- Context Selector

## Required inputs

- `task_profile`
- `current_stage`
- `skill_catalog`
- `budget`

## Optional inputs

- `active_failures`
- `repository_memory`

## Procedure

1. Classify the current stage as understand, localize, reproduce, diagnose, patch, verify, review, or learn.
2. Filter skills whose required inputs are unavailable or whose roles are incompatible.
3. Remove redundant skills with overlapping primary outputs.
4. Select the minimal set that closes the next evidence gap and order by dependency.
5. Re-route after a stage transition or material failure.

## Verification

- Every activated skill has a matching trigger and compatible role.
- No two active skills duplicate the same transformation without an explicit comparison purpose.
- The plan fits the remaining token and interaction budget.

## Stop conditions

- No more than five skills are active for one role.
- The next stage has enough evidence to begin.

## Constraints

- Do not activate the entire library by default.
- Do not route by name similarity alone.

## Allowed tools

- `read_file`
- `text_search`

## Provenance

Synthesized from design patterns in `agentskills-spec`, `deepseek-harness`, `openhands-extensions`, `addy-agent-skills`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.