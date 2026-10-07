import asyncio
import sys

import pytest

from pluginrsi.runtime import InfraError
from pluginrsi.schemas import ProposerConfig
from pluginrsi.search.api_proposer import EvolverAgent
from pluginrsi.search.proposer import CommandProposer
from pluginrsi.search.store import read_json


def test_evolver_persists_transport_failure_without_a_submission(tmp_path):
    class FailingEvolver(EvolverAgent):
        def __init__(self):
            self.request = {"candidate_id": "c000001", "phase": "plugin_mutation"}
            self.output = tmp_path

        async def _run(self):
            raise InfraError("fixture gateway failure")

    with pytest.raises(InfraError):
        asyncio.run(FailingEvolver().run())
    assert read_json(tmp_path / "proposal_failure.json") == {"kind": "infra_error", "detail": "fixture gateway failure"}
    assert not (tmp_path / "proposal_result.json").exists()


@pytest.mark.parametrize("infra", [True, False])
def test_command_proposer_preserves_failure_type_across_process_boundary(tmp_path, infra):
    program = "import json,sys; from pathlib import Path; p=Path(sys.argv[1]).parent; "
    if infra:
        program += "(p/'proposal_failure.json').write_text(json.dumps({'kind':'infra_error','detail':'fixture gateway failure'})); "
    program += "sys.exit(1)"
    proposer = CommandProposer(ProposerConfig(command=[sys.executable, "-c", program]))
    with pytest.raises(InfraError if infra else ValueError):
        asyncio.run(proposer.propose({"output_dir": str(tmp_path)}))


@pytest.mark.parametrize('outcome', ['budget_exhausted', 'success', 'fresh_infra'])
def test_retry_does_not_reuse_previous_failure_marker(tmp_path, outcome):
    prefix = "import json,sys; from pathlib import Path; p=Path(sys.argv[1]).parent; "
    first = prefix + "(p/'proposal_failure.json').write_text(json.dumps({'kind':'infra_error','detail':'old gateway failure'})); sys.exit(1)"
    config = ProposerConfig(command=[sys.executable, '-c', first])
    proposer = CommandProposer(config)
    request = {'output_dir': str(tmp_path)}
    with pytest.raises(InfraError, match='old gateway failure'):
        asyncio.run(proposer.propose(request))
    second = prefix + "assert not (p/'proposal_failure.json').exists(); "
    if outcome == 'success':
        second += "(p/'proposal_result.json').write_text(json.dumps({'hypothesis':'done','changes':[],'new_plugin_refs':[]}))"
    elif outcome == 'fresh_infra':
        second += "(p/'proposal_failure.json').write_text(json.dumps({'kind':'infra_error','detail':'new gateway failure'})); sys.exit(1)"
    else:
        second += "raise RuntimeError('proposal call budget exhausted')"
    config.command = [sys.executable, '-c', second]
    if outcome == 'success':
        assert asyncio.run(proposer.propose(request))['hypothesis'] == 'done'
        assert not (tmp_path / 'proposal_failure.json').exists()
    else:
        error, message = (InfraError, 'new gateway failure') if outcome == 'fresh_infra' else (ValueError, 'exited with status 1')
        with pytest.raises(error, match=message):
            asyncio.run(proposer.propose(request))
