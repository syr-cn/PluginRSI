Fixed execution interfaces (not searchable):

- Workflow: async run(task, runtime, plugins) -> SolveResult. No required base class.
- Task: id, instruction, metadata. Contains public task data, not hidden tests.
- Runtime.model: async complete(messages, tools=None) -> Responses-shaped dict,
  including output (message/reasoning/function_call items) and output_text.
  Each function_call has name, arguments (JSON string), call_id. Supply outputs as
  {"type":"function_call_output","call_id":...,"output":"text"} in the next input.
  Model identity, credentials, retry ceiling and model-call limits are fixed.
- Runtime.environment: async execute(command, timeout_seconds=180) -> ToolResult;
  async upload_file(local_Path, container_path), async download_file(container_path,
  local_Path). Commands execute in the benchmark environment, not the host.
  Each execution starts a fresh non-login shell. Directory changes and virtual
  environment activation do not persist between calls. When imports fail, inspect
  the task's existing interpreters/environments before assuming dependencies are
  unavailable; the default Python may differ from the prepared task environment.
- Runtime.emit(event_name, **JSON_serializable_data) writes a trajectory event.
- Runtime.resource_dir is the read-only harness directory. work_dir is host-local.
- PluginServices provides model, environment, emit, read-only resource_dir and an
  independent writable state_dir for that plugin alias in this task attempt.
- Plugin manifests use schema_version: 1, kind, name, version, entrypoint,
  description, config_schema (JSON Schema), and provenance. Package modules use
  relative imports, and every package includes __init__.py.
- Harness manifests use schema_version: 1, entrypoint and plugins mapping aliases
  to {ref: "kind/name/version", config: {...}}. No graph, model or evaluator fields.
- provenance stores label, sources (source IDs and evidenced titles/URLs),
  upstream_assets, adaptation, and parent_refs (precise existing plugin refs).
  Runtime automatically emits paired plugin/input and plugin/output events for
  standard methods, with invocation_id, alias, exact version and provenance.
- Plugin code may import the fixed SDK and locked dependencies, not other plugins.
  Workflow coordinates cross-plugin calls. Runtime does not impose a solver loop.
