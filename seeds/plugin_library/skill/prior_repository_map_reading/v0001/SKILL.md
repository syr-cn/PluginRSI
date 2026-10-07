---
name: repository-map-reading
description: Use a structural repository map to rank definitions and references without loading full files.
---

# Repository Map Reading

**Internal ID:** `S06`  
**Category:** `localization`  
**Tier:** `optional`  
**Output:** `RankedSymbols`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Planner
- Solver
- Context Selector

## Required inputs

- `repository_map`
- `issue_profile`

## Optional inputs

- `symbol_graph`

## Procedure

1. Match issue concepts to symbol names, signatures, paths, and reference relationships.
2. Prefer public entry points and symbols on short dependency paths to the symptom.
3. Rank a small set of files and symbols with evidence.
4. Request source text only for the highest-ranked candidates.

## Verification

- Each ranked symbol has structural and issue evidence.
- The output is small enough for focused reading.

## Stop conditions

- Five or fewer symbols are ranked or the map is judged insufficient.

## Constraints

- Do not infer implementation behavior from signatures alone.

## Allowed tools

- `repo_map`
- `symbol_search`

## Provenance

Synthesized from design patterns in `aider`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.