# SWE-bench Scope Guard

These rules are always-on for every runtime role, prompt, workflow and skill.

- Do not create a new top-level directory unless the issue explicitly requires it.
- Do not reorganize the repository or refactor unrelated code.
- Do not modify tests merely to hide a failure.
- Do not change dependencies, lockfiles, build files, or CI configuration unless causally necessary.
- Prefer the smallest coherent source patch that satisfies the issue and preserves public behavior.
- Run focused verification before broad test suites.
- Inspect the final diff and revert incidental edits before submission.
- Do not claim success without fresh execution evidence.

## Default interpretation

- A source change is in scope only when it repairs an acceptance criterion, preserves a touched contract, or is required for verification.
- New tests may be written only when the benchmark/evaluation protocol permits them; never alter tests to conceal a failure.
- Large refactors, directory moves, broad formatting, dependency upgrades, release work and documentation overhauls are outside SWE-bench v1.