import json
from pathlib import Path

from pluginrsi.search.api_proposer import EvolverAgent
from pluginrsi.search.loop import Search
from pluginrsi.search.loop import initialize
from test_full_search import config_at, Evaluator, Proposer


def test_local_scaffold_blocks_wrong_paths_and_validates_before_submit(tmp_path):
    cfg = config_at(tmp_path)
    store = initialize(cfg)
    search = Search(cfg, store, Evaluator(store), Proposer())
    request = search.make_request({"id":"c000001","parent":"c000000"}, "plugin_mutation",
        {"id":"e000000","scores":[]}, ["role/coder/v0001","tool/terminal/v0001","skill/debugging/v0001","memory/experience_bank/v0001"], [], alias="coder")
    agent = EvolverAgent(request)
    prefix = request["local_contract"]["plugin_output_prefix"]
    assert prefix == "new_plugins/role/coder/v_c000001"
    assert prefix + "/plugin.yaml" in agent.files
    assert "error:" in agent.tool("write_file", {"path":"child/plugins/role/coder/v_c000001/plugin.yaml","content":"bad"})
    assert "new_plugins/" in agent.tool("write_file", {"path":"library/role/coder/v_c000001/plugin.yaml","content":"bad"})
    assert "error:" in agent.tool("write_file", {"path":"new_plugins/role/other/v_c000001/plugin.yaml","content":"bad"})
    assert not (Path(request["output_dir"]) / "harness/plugins").exists()
    result = {"hypothesis":"test", "changes":["test"], "new_plugin_refs":["role/coder/v_c000001"]}
    original = agent.tool("read_file", {"path":prefix + "/implementation.py"})
    agent.tool("write_file", {"path":prefix + "/implementation.py","content":"def broken("})
    assert "error:" in agent.tool("submit_proposal", result)
    assert not (Path(request["output_dir"]) / "proposal_result.json").exists()
    agent.tool("write_file", {"path":prefix + "/implementation.py","content":original})
    assert agent.tool("submit_proposal", result) == "written"
    agent.tool("write_file", {"path":prefix + "/implementation.py","content":"def broken("})
    assert agent.submission_error(result) is not None
