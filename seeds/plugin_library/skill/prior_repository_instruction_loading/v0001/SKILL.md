---
name: repository-instruction-loading
description: Find and apply repository-level instructions such as AGENTS.md, CONTRIBUTING, test commands, and local conventions.
---

# Repository Instruction Loading

**Internal ID:** `S03`  
**Category:** `understanding`  
**Tier:** `core`  
**Output:** `RepositoryRules`

## Activate when

- The workflow reaches the `understanding` stage and the required inputs are present.

## Applicable roles

- Task Analyzer
- Solver
- Verifier

## Required inputs

- `repository_root`

## Optional inputs

- `issue_path_hints`

## Procedure

1. Inspect root-level instruction files and configuration entry points.
2. Follow nested instructions only for files inside their scope.
3. Extract build, test, style, generated-file, and do-not-edit rules.
4. Record conflicts and precedence.

## Verification

- Each rule includes its source path and scope.
- Commands are copied exactly or marked inferred.

## Stop conditions

- All instructions governing likely changed files have been loaded.

## Constraints

- Do not treat README prose as a hard rule unless clearly operational.
- Do not scan the entire repository.

## Allowed tools

- `list_tree`
- `read_file`
- `text_search`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `openhands-extensions`, `deepseek-harness`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.