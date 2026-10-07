import asyncio
from collections import Counter
import pytest
from pluginrsi.schemas import RunConfig
from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.search.store import Store,write_json,read_json
from pluginrsi.search.selection import apply_quality_policy,is_valid

def fixture(tmp,fast=False):
 c=RunConfig(seed=tmp,plugin_library=tmp,run_dir=tmp/'run',search=dict(feedback_batch_size=2,local_failure_tasks=1,max_task_rollouts=200),evaluation=dict(benchmark='fixture',feedback_tasks=tmp/'tasks',selection_tasks=tmp/'tasks',concurrency=20,startup_stagger_max_seconds=.001,min_valid_ratio=.95,full_min_valid_ratio=.95,accept_partial_results=fast,eager_infra_retries=True,infra_retry_attempts=1,evaluation_timeout_seconds=2),solver=dict(model='fixture'),proposer=dict(command=['unused']))
 s=Store(c.run_dir);s.save();return c,s

def seed_record(s,valid,attempt):
 rows=[]
 for i in range(20):
  row=dict(task_id=str(i),score=0,status='completed' if i<valid else 'infra_error')
  write_json(s.root/f'evaluations/e000000/tasks/{i}/attempt_{attempt}/result.json',row);rows.append(row)
 r=apply_quality_policy(dict(id='e000000',candidate_id='c000000',purpose='selection',task_ids=[str(i) for i in range(20)],scores=rows,attempts={str(i):attempt for i in range(20)},rollouts_started=20*attempt),.95)
 write_json(s.root/'evaluations/e000000/evaluation.json',r)
 s.state.update(evaluation_keys={'seed:selection':'e000000'},next_evaluation=1,rollouts_started=20*attempt);s.save()

@pytest.mark.parametrize('valid,attempt',[(19,1),(18,2),(18,3)])
def test_accepted_or_exhausted_cache_never_retries(tmp_path,monkeypatch,valid,attempt):
 c,s=fixture(tmp_path);seed_record(s,valid,attempt)
 async def forbidden(*a,**k):raise AssertionError('unexpected worker')
 monkeypatch.setattr('pluginrsi.evaluation.runner.run_command',forbidden)
 for _ in range(2):
  r=asyncio.run(Evaluator(c,Store(s.root),retry_infra=True).evaluate('c000000',[dict(id=str(i)) for i in range(20)],'selection','seed:selection'))
  assert is_valid(r)==(valid==19) and r['valid_task_count']==valid
 assert Store(s.root).state['rollouts_started']==20*attempt

@pytest.mark.parametrize('fast',[False,True])
def test_resume_cannot_create_third_attempt(tmp_path,monkeypatch,fast):
 c,s=fixture(tmp_path,fast);seed_record(s,18,1);calls=[]
 async def command(command,cwd,log_path,timeout,env=None):
  calls.append(read_json(cwd/'request.json')['task']['id']);write_json(cwd/'result.json',dict(score=0,status='infra_error'));return 0
 monkeypatch.setattr('pluginrsi.evaluation.runner.run_command',command)
 for _ in range(2):
  r=asyncio.run(Evaluator(c,Store(s.root),retry_infra=True).evaluate('c000000',[dict(id=str(i)) for i in range(20)],'selection','seed:selection'))
  assert r['status']=='infra_error'
 assert sorted(calls)==['18','19'] and Store(s.root).state['rollouts_started']==22


def test_settled_batch_with_one_failure_does_not_retry(tmp_path,monkeypatch):
 c,s=fixture(tmp_path);calls=Counter()
 async def command(command,cwd,log_path,timeout,env=None):
  tid=read_json(cwd/'request.json')['task']['id'];calls[tid]+=1
  write_json(cwd/'result.json',dict(score=0,status='infra_error' if tid=='19' else 'completed'));return 0
 monkeypatch.setattr('pluginrsi.evaluation.runner.run_command',command)
 r=asyncio.run(Evaluator(c,s).evaluate('c000000',[dict(id=str(i)) for i in range(20)],'selection','seed:selection'))
 assert is_valid(r) and r['valid_task_count']==19 and sum(calls.values())==20
