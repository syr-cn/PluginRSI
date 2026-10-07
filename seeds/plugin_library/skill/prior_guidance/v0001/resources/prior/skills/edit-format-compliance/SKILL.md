---
name: edit-format-compliance
description: Produce syntactically valid edit blocks or unified diffs that apply cleanly.
---

# Edit-format Compliance

**Internal ID:** `S21`  
**Category:** `patching`  
**Tier:** `optional`  
**Output:** `ValidatedEdits`

## Activate when

- The workflow reaches the `patching` stage and the required inputs are present.

## Applicable roles

- Editor
- Solver

## Required inputs

- `planned_edits`
- `target_file_contents`
- `edit_format`

## Procedure

1. Use exact current context around every edit.
2. Avoid overlapping or ambiguous edit blocks.
3. Apply the edit and inspect parser/application errors.
4. Regenerate only the failed edit rather than rewriting unrelated content.

## Verification

- All edits apply cleanly to the current revision.
- The resulting diff matches intended semantics.

## Stop conditions

- The patch applies and source files remain syntactically parseable.

## Constraints

- Do not rewrite whole files for a small edit unless the format requires it.

## Allowed tools

- `apply_patch`
- `git_diff`
- `read_range`

## Provenance

Synthesized from design patterns in `aider`, `swe-agent`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.