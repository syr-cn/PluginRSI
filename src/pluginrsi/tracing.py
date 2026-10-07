from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path

ORIGIN = ContextVar("pluginrsi_trace_origin", default={})
PLUGIN_METHODS = {"role": ("render",), "skill": ("load",), "tool": ("schema", "execute"),
                  "memory": ("update", "retrieve")}


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"trace value is not JSON serializable: {type(value).__name__}")


@contextmanager
def origin(value):
    token = ORIGIN.set({**ORIGIN.get(), **value})
    try:
        yield
    finally:
        ORIGIN.reset(token)


def instrument(plugin, manifest, alias, runtime):
    producer = {"kind": manifest.kind, "plugin_alias": alias, "plugin_ref": manifest.ref,
                "label": manifest.provenance.get("label", manifest.ref), "provenance": manifest.provenance}

    def wrap(method, name):
        def begin(args, kwargs):
            runtime.plugin_calls += 1
            invocation = f"plugin_{runtime.plugin_calls:06d}"
            scope = {"producer": producer, "invocation_id": invocation, "method": name}
            return invocation, scope

        if inspect.iscoroutinefunction(method):
            @wraps(method)
            async def asynchronous(*args, **kwargs):
                invocation, scope = begin(args, kwargs)
                with origin(scope):
                    runtime.emit("plugin/input", input={"args": args, "kwargs": kwargs})
                    try:
                        result = await method(*args, **kwargs)
                    except Exception as error:
                        runtime.emit("plugin/error", error_type=type(error).__name__, message=str(error))
                        raise
                    runtime.emit("plugin/output", output=result)
                    runtime.plugin_outputs.append({"invocation_id": invocation, **producer, "method": name})
                    return result
            return asynchronous

        @wraps(method)
        def synchronous(*args, **kwargs):
            invocation, scope = begin(args, kwargs)
            with origin(scope):
                runtime.emit("plugin/input", input={"args": args, "kwargs": kwargs})
                try:
                    result = method(*args, **kwargs)
                except Exception as error:
                    runtime.emit("plugin/error", error_type=type(error).__name__, message=str(error))
                    raise
                runtime.emit("plugin/output", output=result)
                runtime.plugin_outputs.append({"invocation_id": invocation, **producer, "method": name})
                return result
        return synchronous

    for method in PLUGIN_METHODS[manifest.kind]:
        setattr(plugin, method, wrap(getattr(plugin, method), method))
    return plugin
