import json

from pluginrsi.contracts import Memory, Role, Skill, SolveResult, Tool
from pluginrsi.runtime import InvalidToolCall

FINISH_SCHEMA = {
    'type': 'function', 'name': 'finish', 'description': 'Submit the final answer and end the task.',
    'parameters': {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                   'required': ['answer'], 'additionalProperties': False},
}


class Workflow:
    async def run(self, task, runtime, plugins):
        tools = {alias: plugin for alias, plugin in plugins.items() if isinstance(plugin, Tool)}
        memories = [plugin for plugin in plugins.values() if isinstance(plugin, Memory)]
        guidance = '\n'.join(plugin.load({}).instructions for plugin in plugins.values() if isinstance(plugin, Skill))
        system = '\n'.join(plugin.render({'guidance': guidance}) for plugin in plugins.values() if isinstance(plugin, Role))
        schemas = [dict(plugin.schema(), name=alias) for alias, plugin in tools.items()] + [FINISH_SCHEMA]
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': task.instruction}]
        turn = 0
        while True:
            if turn:
                recalled = [item.content for memory in memories for item in await memory.retrieve({'query': task.instruction})]
                if recalled:
                    messages.append({'role': 'user', 'content': 'Relevant task-local observations:\n' + '\n'.join(recalled)})
            response = await runtime.model.complete(messages, schemas)
            messages.extend(response['output'])
            calls = [item for item in response['output'] if item['type'] == 'function_call']
            if not calls:
                messages.append({'role': 'user', 'content': 'Continue using tools, or call finish(answer=...).'})
            for call in calls:
                if call['name'] not in tools and call['name'] != 'finish':
                    raise InvalidToolCall('Unknown tool')
                try:
                    arguments = json.loads(call['arguments'])
                except (TypeError, ValueError) as error:
                    raise InvalidToolCall('Tool arguments must be JSON') from error
                if not isinstance(arguments, dict):
                    raise InvalidToolCall('Tool arguments must be an object')
                if call['name'] == 'finish':
                    if not isinstance(arguments.get('answer'), str) or not arguments['answer'].strip():
                        raise InvalidToolCall('finish requires a nonempty answer')
                    return SolveResult(arguments['answer'])
                result = await tools[call['name']].execute(arguments)
                messages.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': result.content})
                for memory in memories:
                    await memory.update({'content': result.content, 'metadata': {'turn': turn, 'tool': call['name']}})
            turn += 1
