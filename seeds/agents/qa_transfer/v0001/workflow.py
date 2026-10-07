import json

from pluginrsi.contracts import Memory, Role, Skill, SolveResult, Tool
from pluginrsi.runtime import InvalidToolCall

MAX_TURNS = 20
FINISH_SCHEMA = {
    'type': 'function', 'name': 'finish', 'description': 'Submit the final answer in the required format.',
    'parameters': {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                   'required': ['answer'], 'additionalProperties': False},
}


class Workflow:
    async def run(self, task, runtime, plugins):
        tools = {alias: plugin for alias, plugin in plugins.items() if isinstance(plugin, Tool)}
        if 'finish' in tools:
            raise ValueError('finish is reserved for answer submission')
        memories = [plugin for plugin in plugins.values() if isinstance(plugin, Memory)]
        context = {'answer_mode': task.metadata['answer_mode'], 'choices': task.metadata['choices']}
        guidance = '\n'.join(plugin.load(context).instructions for plugin in plugins.values() if isinstance(plugin, Skill))
        system = '\n'.join(plugin.render({'guidance': guidance}) for plugin in plugins.values() if isinstance(plugin, Role))
        schemas = [dict(plugin.schema(), name=alias) for alias, plugin in tools.items()] + [FINISH_SCHEMA]
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': task.instruction}]
        allowed_keys = {choice['key'] for choice in context['choices']}
        for turn in range(MAX_TURNS):
            if turn:
                recalled = [item.content for memory in memories for item in await memory.retrieve({'query': task.instruction})]
                if recalled:
                    messages.append({'role': 'user', 'content': 'Relevant task-local observations:\n' + '\n'.join(recalled)})
            response = await runtime.model.complete(messages, schemas)
            messages.extend(response['output'])
            calls = [item for item in response['output'] if item['type'] == 'function_call']
            if not calls:
                messages.append({'role': 'user', 'content': 'Call finish(answer=...) to submit, or use a tool if needed.'})
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
                    answer = arguments.get('answer')
                    valid = isinstance(answer, str) and bool(answer.strip())
                    if context['answer_mode'] in ('choice', 'label'):
                        valid = valid and answer.strip() in allowed_keys
                    if valid:
                        return SolveResult(answer.strip())
                    messages.append({'type': 'function_call_output', 'call_id': call['call_id'],
                                     'output': 'Invalid answer format. Submit a nonempty answer; for choice/label use exactly one allowed key.'})
                    continue
                result = await tools[call['name']].execute(arguments)
                messages.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': result.content})
                for memory in memories:
                    await memory.update({'content': result.content,
                                         'metadata': {'turn': turn, 'tool': call['name'], 'is_error': result.is_error}})
        return SolveResult('', termination_reason='turn_limit')
