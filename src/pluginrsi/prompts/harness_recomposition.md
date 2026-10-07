Build a better complete Harness by integrating useful ideas from this round's
plugin experiments. Prioritize newly published plugins with positive local score gains;
read their implementations and relevant parent/child trajectories to understand which
failures they repaired, any regressions, and when their mechanisms should help.
Local gains are evidence for integration, not a guarantee of full-set improvement.
Redesign workflow control flow, context and plugin composition so selected plugins are
actually used where they can help. Combine compatible mechanisms; avoid blindly stacking
plugins or preserving the incumbent assembly by default. Use rejected attempts to avoid
repeating failures. In hypothesis, name the selected plugin refs, supporting local results
and how the workflow will use them; explain if no new plugin is suitable.
Do not implement explicit content hashing or digests. Use direct string equality for deduplication.
You may rewrite the entire child workflow package, helpers, resources and plugin assembly.
Use only frozen allowed_plugin_refs. Do not create or modify plugin packages in this phase.
Do not change the model, runtime, evaluator, resource ceilings or hidden tests; do not install dependencies.
Do not hard-code task answers. Deliver executable files and submit_proposal with new_plugin_refs=[].
