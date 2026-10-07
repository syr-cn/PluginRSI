---
name: environment-blocker-diagnosis
description: Separate environment/setup failures from product failures and produce a bounded recovery plan.
---

# Environment Blocker Diagnosis

**Internal ID:** `S29`  
**Category:** `recovery`  
**Tier:** `core`  
**Output:** `EnvironmentDiagnosis`

## Activate when

- The workflow reaches the `recovery` stage and the required inputs are present.

## Applicable roles

- Critic
- Solver
- Termination Controller

## Required inputs

- `command`
- `failure_output`
- `environment_info`

## Optional inputs

- `repository_memory`

## Procedure

1. Classify missing dependency, unavailable service, platform mismatch, timeout, resource exhaustion, or configuration error.
2. Check repository-provided setup instructions before modifying files.
3. Propose the least invasive recovery or a blocked status.
4. Record which evidence cannot be obtained.

## Verification

- The blocker classification explains the command failure.
- Recovery does not alter project semantics.

## Stop conditions

- Execution resumes or the task is marked blocked with evidence.

## Constraints

- Do not patch source code to bypass an environment issue.
- Do not install or update arbitrary dependencies.

## Allowed tools

- `shell_exec`
- `read_file`
- `inspect_failure`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `swe-agent`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.