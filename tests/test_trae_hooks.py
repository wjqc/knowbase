import json
import os
import subprocess
import sys

from knowbase import index
from knowbase.adapters import host_events
from test_inflight_host_contract import enable, event


def test_trae_tool_event_has_own_identity(memory_repo,tmp_path):
    enable(memory_repo,tmp_path)
    data=event(tmp_path,tmp_path/'missing.jsonl')
    data['tool_name']='RunCommand';data['tool_input']={'command':'echo test'}
    result=host_events.handle(data,event='tool-before',style='trae')
    assert not result.get('error'),result
    c=index.connect(memory_repo)
    assert c.execute("SELECT COUNT(*) FROM context_sessions WHERE host_profile='trae'").fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM context_sessions WHERE host_profile='claude'").fetchone()[0]==0
    c.close()


def test_trae_cli_emits_official_context_envelope(memory_repo,tmp_path):
    enable(memory_repo,tmp_path)
    data=event(tmp_path,tmp_path/'missing.jsonl')
    data['tool_name']='RunCommand';data['tool_input']={'command':'pytest tests/orders.py'}
    result=subprocess.run([sys.executable,'-m','knowbase','hook','tool-before','--style','trae'],
                          input=json.dumps(data),text=True,capture_output=True,env=os.environ.copy())
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)['hookSpecificOutput']
    assert output['hookEventName']=='PreToolUse' and output['additionalContext']
    assert 'permissionDecision' not in output


def test_trae_failure_uses_post_tool_result(memory_repo,tmp_path):
    enable(memory_repo,tmp_path)
    data=event(tmp_path,tmp_path/'missing.jsonl')
    data.update({'tool_name':'RunCommand','tool_response':{'exit_code':1,'stderr':'ORDER_RULE_INVALID'}})
    result=host_events.handle(data,event='tool-after',style='trae')
    assert not result.get('error'),result
    c=index.connect(memory_repo)
    assert c.execute("SELECT COUNT(*) FROM context_events WHERE host_profile='trae' AND trigger='new_failure'").fetchone()[0]==1
    c.close()
