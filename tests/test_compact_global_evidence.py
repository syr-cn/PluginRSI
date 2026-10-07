import json
from pathlib import Path

from pluginrsi.search.api_proposer import EvolverAgent


def test_global_index_preserves_all_scores_and_materializes_details_on_demand(tmp_path):
    feedback=tmp_path/'run/evaluations/e1';output=tmp_path/'candidate';(output/'harness').mkdir(parents=True)
    rows=[]
    for i in range(400):
        tid=f'repo{i%5}__task{i}'
        trace=feedback/tid/'trajectory.jsonl';trace.parent.mkdir(parents=True)
        trace.write_text(json.dumps(dict(event='model/request',request=dict(input=[dict(role='user',content=f'Issue {i}')])))+'\n')
        rows.append(dict(task_id=tid,score=i%2,status='completed' if i<399 else 'infra_error',trajectory=f'{tid}/trajectory.jsonl'))
    request=dict(output_dir=str(output),parent_dir=str(output/'harness'),library_dirs=[],allowed_plugin_refs=[],
        feedback_dir=str(feedback),feedback=dict(scores=rows),phase='harness_recomposition',candidate_id='c1',version='v_c1',
        instructions='Integrate evidence',history={},contracts='',runtime_contract='',compact_evidence=True)
    (tmp_path/'run/config.yaml').write_text('solver:\n  max_calls: 20\n  max_output_tokens: 8192\nevaluation:\n  task_timeout_seconds: 600\n')
    agent=EvolverAgent(request);public=agent.prepare_context()
    assert public['feedback_index']['rows']==[[r['task_id'],r['score'],r['status']] for r in rows]
    assert len(public['feedback_examples'])==4
    assert public['feedback_overview']['status_counts']['infra_error']==1
    assert len(list(feedback.glob('*/proposer_compact_summary.json')))==4
    assert len(json.dumps(public).encode())<50000
    target=next(r['task_id'] for r in rows if r['task_id'] not in {s['task_id'] for s in public['feedback_examples']})
    response=agent.tool('read_file',dict(path=f'feedback/{target}.summary.json'))
    assert json.loads(response)['task_id']==target
    assert len(list(feedback.glob('*/proposer_compact_summary.json')))==5
    assert not any(name.endswith('.summary.json') for name in agent.initial_files())
    assert agent.tool('read_file',dict(path='unlisted/file'))=='error: file is not readable'
