---
name: repository-orientation
description: Build a compact map of project structure, build system, tests, and likely entry points.
---

# Repository Orientation

**Internal ID:** `S05`  
**Category:** `localization`  
**Tier:** `core`  
**Output:** `RepositoryOrientation`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Planner
- Solver

## Required inputs

- `repository_root`
- `issue_profile`

## Optional inputs

- `repository_rules`

## Procedure

1. Identify language, package manager, build files, and test framework.
2. List only top-level and issue-relevant subtrees.
3. Locate public entry points and likely modules from issue terminology.
4. Find existing focused test commands and development conventions.
5. Rank the next files or symbols to inspect.

## Verification

- The map is bounded and linked to the issue.
- At least one test or execution entry point is identified or marked unknown.

## Stop conditions

- A short ranked inspection plan exists.

## Constraints

- Do not recursively read all files.
- Do not equate naming similarity with causality.

## Allowed tools

- `list_tree`
- `read_file`
- `text_search`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `aider`, `agentless`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.