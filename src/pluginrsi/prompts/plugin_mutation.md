Use this local experiment to invent a plugin-level solution to a concrete failure
in the supplied trajectories. Read summaries, then inspect relevant full trace ranges
and plugin code to identify where the attempt went wrong; check successes for regressions.
Develop one transferable mechanism that could fix that failure. You may revise the
existing implementation or replace it from scratch; the copied code is only a scaffold.
Test the idea through the plugin instance named by local_contract.alias.
Do not implement explicit content hashing or digests. Use direct string equality for deduplication.
The controller has copied the source plugin into local_contract.plugin_output_prefix.
Edit that new_plugins/<kind>/<name>/<version>/ package directly. library/ is READ-ONLY.
Never write plugins under child/plugins/ or any other child/ subdirectory.
Keep every workflow file byte-for-byte unchanged. In harness.yaml change only that alias's
plugin ref to the new version; keep its config and all other manifest values unchanged.
Create exactly one new plugin with the same kind/name as the assigned source ref and the
controller-assigned version. Implement a transferable improvement, not task-specific answers.
Other plugins and the shared runtime/evaluator are immutable. Keep the public interface
and existing instance configuration compatible. Do not install dependencies or inspect hidden tests.
Use local feedback and your previous attempts, including failures, to refine the mechanism.
In hypothesis, cite the task/trace evidence, the failure mechanism and the expected repair.
Deliver actual files and call submit_proposal with hypothesis, changes:list[string], new_plugin_refs.
New plugin provenance requires label, sources, upstream_assets, adaptation and parent_refs.
