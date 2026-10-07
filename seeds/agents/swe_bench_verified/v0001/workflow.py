import json

from pluginrsi.contracts import Memory, Role, Skill, SolveResult, Tool

MAX_TURNS = 20


class Workflow:
    async def run(self, task, runtime, plugins):
        tools = {alias: plugin for alias, plugin in plugins.items() if isinstance(plugin, Tool)}
        memories = [plugin for plugin in plugins.values() if isinstance(plugin, Memory)]
        guidance = "\n".join(plugin.load({}).instructions for plugin in plugins.values() if isinstance(plugin, Skill))
        system = "\n".join(plugin.render({"guidance": guidance}) for plugin in plugins.values() if isinstance(plugin, Role))
        schemas = [dict(plugin.schema(), name=alias) for alias, plugin in tools.items()]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task.instruction}]
        for turn in range(MAX_TURNS):
            if turn:
                recalled = [item.content for memory in memories for item in await memory.retrieve({"query": task.instruction})]
                if recalled:
                    messages.append({"role": "user", "content": "Relevant task-local observations:\n" + "\n".join(recalled)})
            response = await runtime.model.complete(messages, schemas)
            messages.extend(response["output"])
            calls = [item for item in response["output"] if item["type"] == "function_call"]
            if not calls:
                return SolveResult(response["output_text"])
            for call in calls:
                result = await tools[call["name"]].execute(json.loads(call["arguments"]))
                messages.append({"type": "function_call_output", "call_id": call["call_id"], "output": result.content})
                for memory in memories:
                    await memory.update({"content": result.content, "metadata": {"turn": turn, "tool": call["name"]}})
        return SolveResult("Turn limit reached.", termination_reason="turn_limit")
