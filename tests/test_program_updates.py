"""Program updates are independent of knowledge Git. All remotes here are local fixtures."""
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from knowbase import config,integration
from knowbase.updates.engine import Engine,UpdateError,Lock,atomic,cards,ledger_snapshot,module_hash


def git(repo,*args):
    r=subprocess.run(['git','-C',str(repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid',*args],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    return r.stdout.strip()


@pytest.fixture
def update_world(tmp_path):
    home=tmp_path/'home';root=home/'.knowbase/runtime';root.mkdir(parents=True)
    python=root/('venv/Scripts/python.exe' if os.name=='nt' else 'venv/bin/python');python.parent.mkdir(parents=True);python.touch()
    repo=home/'knowledge';repo.mkdir();(repo/'fixture.md').write_text('unchanged knowledge')
    c=sqlite3.connect(repo/'memory.db');c.execute('CREATE TABLE usage_log(id INTEGER PRIMARY KEY,detail TEXT)');c.execute("INSERT INTO usage_log(detail) VALUES('preserve me')");c.commit();c.close()
    cp=home/'.knowbase/config.json';atomic(cp,{'repo_path':str(repo),'team':{'data_dir':str(home/'.knowbase/team')}})
    source=tmp_path/'program';source.mkdir();git(source,'init','-q','-b','main')
    pkg=source/'knowbase';pkg.mkdir();(pkg/'__init__.py').write_text("__version__='0.2.0rc8'\n")
    (source/'pyproject.toml').write_text('[project]\nname="knowbase"\nversion="0.2.0rc8"\nrequires-python=">=3.10"\n[tool.knowbase.updates]\nprotocol=1\n')
    for name in ['engine.py','entry.py','bridge.py']:
        p=pkg/'updates'/name;p.parent.mkdir(exist_ok=True);p.write_text('# fixture\n')
    git(source,'add','-A');git(source,'commit','-qm','baseline');old=git(source,'rev-parse','HEAD')
    installed=tmp_path/'installed';__import__('shutil').copytree(pkg,installed/'knowbase')
    atomic(root/'active.json',{'version':'0.2.0rc8','source_commit':old,'build_id':module_hash(pkg),'project_sha256':hashlib.sha256((source/'pyproject.toml').read_bytes()).hexdigest()})
    settings=home/'.knowbase/updates.json';atomic(settings,{'home':str(home),'enabled':True,'runtime_python':str(python),
        'source_url':source.as_uri(),'allow_file_source':True,'uv_path':sys.executable,'config_path':str(cp),'manage_worker':False,'max_wait_seconds':1})
    return home,root,source,settings,installed


def test_source_cache_does_not_checkout_or_modify_knowledge(update_world):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    assert e.fetch()==git(source,'rev-parse','HEAD')
    assert (home/'knowledge/fixture.md').read_text()=='unchanged knowledge'
    assert not (home/'knowledge/.git').exists()


def test_non_fast_forward_and_credential_url_are_rejected(update_world):
    home,root,source,settings,installed=update_world;e=Engine(settings);e.fetch()
    git(source,'checkout','--orphan','replacement');git(source,'commit','-am','rewrite');git(source,'branch','-M','main')
    with pytest.raises(UpdateError,match='non_fast_forward'):e.fetch()
    data=json.loads(settings.read_text());data['source_url']='https://user:secret@example.invalid/code';atomic(settings,data)
    with pytest.raises(UpdateError,match='invalid_program_source'):Engine(settings)


def test_updater_lock_and_append_only_user_ledger(update_world):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    with Lock(e.directory/'.lock'):
        with pytest.raises(UpdateError,match='already_running'):
            with Lock(e.directory/'.lock'):pass
    before=ledger_snapshot(home/'knowledge/memory.db')
    c=sqlite3.connect(home/'knowledge/memory.db');c.execute("INSERT INTO usage_log(detail) VALUES('new feedback must not disappear')");c.commit();c.close()
    assert ledger_snapshot(home/'knowledge/memory.db',before)!=before


def test_unknown_package_version_or_update_protocol_cannot_be_published(update_world):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    (source/'knowbase/__init__.py').write_text("__version__='0.2.0rc9'\n")
    with pytest.raises(UpdateError,match='package_version_mismatch'):e.source_info(source)


def test_managed_profiles_migrate_and_preserve_other_servers_and_hook_timeouts(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'.knowbase/config.json')
    changes=integration.plan(['zcode','claude-code','trae','codex','opencode'],tmp_path)
    integration.apply(changes,tmp_path)
    z=tmp_path/'.zcode/cli/config.json';doc=json.loads(z.read_text());doc['mcp']['servers']['other']={'command':'keep'}
    doc['hooks']['events']['Stop'][0]['hooks'][0]['timeoutMs']=777;z.write_text(json.dumps(doc))
    integration.apply(integration.plan(['zcode','claude-code','trae','codex','opencode'],tmp_path,managed=True),tmp_path)
    doc=json.loads(z.read_text());assert doc['mcp']['servers']['other']=={'command':'keep'}
    assert doc['mcp']['servers']['knowbase']['args']==['-I',str(tmp_path/'.knowbase/runtime/mcp_bridge.py')]
    assert doc['hooks']['events']['Stop'][0]['hooks'][0]['timeoutMs']==777
    assert 'entry.py' in doc['hooks']['events']['Stop'][0]['hooks'][0]['args'][1]
    assert integration.plan(['zcode','claude-code','trae','codex','opencode'],tmp_path,managed=True)==[]


def test_hook_barrier_never_spools_prompt_or_secret(tmp_path):
    root=tmp_path/'runtime';root.mkdir();atomic(root/'update-drain.json',{'phase':'pause_backends'})
    source=Path(__file__).resolve().parents[1]
    code="import sys;sys.path.insert(0,sys.argv.pop(1));from knowbase.updates.entry import main;raise SystemExit(main())"
    payload={'session_id':'session-1','cwd':str(tmp_path),'prompt':'SYNTHETIC-SECRET-MUST-NOT-PERSIST','tool_input':{'password':'SYNTHETIC-SECRET-MUST-NOT-PERSIST'}}
    r=subprocess.run([sys.executable,'-I','-c',code,str(source),'hook','stop','--style','zcode'],
        env={**os.environ,'KNOWBASE_RUNTIME_ROOT':str(root)},input=json.dumps(payload),capture_output=True,text=True)
    assert r.returncode==0 and r.stdout==''
    records=list((root/'deferred-hooks').glob('*.json'));assert len(records)==1
    assert 'SYNTHETIC-SECRET' not in records[0].read_text()


FAKE_BACKEND=r'''
import json,sys,time,pathlib
version=pathlib.Path(sys.argv[1]).read_text().strip()
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 if m['method']=='initialize':
  result={'protocolVersion':m['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':version},'instructions':'[knowbase-build:'+version+']'}
 else:
  if m['method']=='long':pathlib.Path(sys.argv[2]).write_text('started');time.sleep(.4)
  result={'version':version}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


def test_bridge_keeps_same_host_connection_and_drains_inflight_before_switch(tmp_path):
    root=tmp_path/'runtime';root.mkdir();version=tmp_path/'version';version.write_text('a'*16)
    started=tmp_path/'started';backend=tmp_path/'backend.py';backend.write_text(FAKE_BACKEND)
    source=Path(__file__).resolve().parents[1]
    code="import sys;sys.path.insert(0,sys.argv.pop(1));from knowbase.updates.bridge import run;run(backend_command=sys.argv[1:])"
    proc=subprocess.Popen([sys.executable,'-I','-c',code,str(source),sys.executable,'-u',str(backend),str(version),str(started)],
        env={**os.environ,'KNOWBASE_RUNTIME_ROOT':str(root)},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    messages=queue.Queue()
    threading.Thread(target=lambda:[messages.put(json.loads(line)) for line in proc.stdout],daemon=True).start()
    def send(m):proc.stdin.write(json.dumps({'jsonrpc':'2.0',**m})+'\n');proc.stdin.flush()
    def receive(i):
        end=time.monotonic()+8
        while time.monotonic()<end:
            m=messages.get(timeout=max(.01,end-time.monotonic()))
            if m.get('id')==i:return m
        pytest.fail('bridge response missing')
    try:
        send({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}});assert receive(1)['result']['serverInfo']['version']=='a'*16
        send({'method':'notifications/initialized'});send({'id':2,'method':'long'})
        end=time.monotonic()+5
        while not started.exists() and time.monotonic()<end:time.sleep(.02)
        assert started.exists();atomic(root/'update-drain.json',{'phase':'pause_backends'})
        send({'id':3,'method':'value'})
        assert receive(2)['result']['version']=='a'*16
        end=time.monotonic()+5
        while time.monotonic()<end:
            state=__import__('knowbase.updates.engine',fromlist=['read']).read(root/'bridges'/f'{proc.pid}.json')
            if state.get('phase')=='paused':break
            time.sleep(.02)
        assert state['phase']=='paused';version.write_text('b'*16);(root/'update-drain.json').unlink()
        assert receive(3)['result']['version']=='b'*16
        assert proc.poll() is None  # no host-side reconnect/initialize happened
    finally:
        proc.stdin.close()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()


def test_busy_legacy_client_defers_without_killing_it(update_world,monkeypatch):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    monkeypatch.setattr(e,'processes',lambda:[(987654,str(e.python)+' -I -m knowbase serve')])
    with pytest.raises(UpdateError,match='reconnect_required'):e.quiesce()
    assert json.loads(e.drain.read_text())['phase']=='drain_requests'


def test_different_store_bridge_blocks_global_package_update(update_world,monkeypatch):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    monkeypatch.setattr(e,'live_bridges',lambda:[{'config_path':str(home/'other.json')}])
    with pytest.raises(UpdateError,match='additional_config'):e.quiesce()


def test_rollback_never_restores_over_new_evidence(update_world,tmp_path):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    journal={'backup':str(home/'.knowbase/backups/auto-update-fixture'),
             'ledger':ledger_snapshot(home/'knowledge/memory.db'),'cards':cards(home/'knowledge')}
    c=sqlite3.connect(home/'knowledge/memory.db');c.execute("INSERT INTO usage_log(detail) VALUES('arrived after backup')");c.commit();c.close()
    with pytest.raises(UpdateError,match='overwrite_new_data'):e.rollback(journal,tmp_path)
    assert sqlite3.connect(home/'knowledge/memory.db').execute('SELECT COUNT(*) FROM usage_log').fetchone()[0]==2


def test_scheduler_failure_restores_host_configuration_and_settings(tmp_path,monkeypatch):
    from knowbase.updates import control
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'.knowbase/config.json')
    monkeypatch.setattr(control.shutil,'which',lambda _:sys.executable)
    integration.apply(integration.plan(['zcode'],tmp_path),tmp_path)
    profile=tmp_path/'.zcode/cli/config.json';before=profile.read_bytes()
    settings=tmp_path/'.knowbase/updates.json';atomic(settings,{'enabled':False,'preserved':'identity'})
    settings_before=settings.read_bytes()
    marker=tmp_path/'.knowbase/updates/source.json';atomic(marker,{'source_url':'https://old.invalid/code.git'})
    marker_before=marker.read_bytes()
    def failed(**kwargs):raise RuntimeError('scheduler fixture rejected installation')
    monkeypatch.setattr(control,'install_schedule',failed)
    with pytest.raises(RuntimeError,match='scheduler'):control.enable('https://example.invalid/program.git',agents=['zcode'])
    assert profile.read_bytes()==before and settings.read_bytes()==settings_before
    assert marker.read_bytes()==marker_before


def test_maintenance_hook_without_transcript_leaves_honest_gap(memory_repo,monkeypatch,tmp_path):
    from knowbase import index
    from knowbase.updates.recovery import recover_hooks
    root=Path(os.environ['KNOWBASE_RUNTIME_ROOT']);path=root/'deferred-hooks/event.json'
    record={'config_path':str(config.CONFIG_PATH),'session_id':'missing-session','host':'zcode','event':'stop'}
    atomic(path,record);conn=index.connect(memory_repo)
    try:
        result=recover_hooks(memory_repo,conn,config.load_config())
        assert result=={'recovered':0,'gaps':1} and not path.exists()
        assert conn.execute("SELECT note FROM capture_gaps").fetchone()[0]=='program_update_capture_gap'
        atomic(path,record);recover_hooks(memory_repo,conn,config.load_config())
        assert conn.execute('SELECT COUNT(*) FROM capture_gaps').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    finally:conn.close()


def test_maintenance_hook_recovers_only_bound_explicit_transcript(memory_repo,monkeypatch,tmp_path):
    from knowbase import index
    from knowbase.updates.recovery import recover_hooks
    from knowbase.adapters import host_events
    root=Path(os.environ['KNOWBASE_RUNTIME_ROOT']);path=root/'deferred-hooks/event.json'
    transcript=tmp_path/'explicit.jsonl';transcript.write_text('{}')
    atomic(path,{'config_path':str(config.CONFIG_PATH),'session_id':'bound','host':'zcode','transcript_path':str(transcript)})
    conn=index.connect(memory_repo)
    conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,updated_at) VALUES('zcode','bound','orders','fixture')");conn.commit()
    seen=[]
    monkeypatch.setattr(host_events,'capture_increment',lambda *a,**k:seen.append((a,k)) or {'capture_status':'queued'})
    try:
        assert recover_hooks(memory_repo,conn,config.load_config())=={'recovered':1,'gaps':0}
        assert seen[0][0][2:]==(str(transcript),'orders','zcode') and seen[0][1]=={'automatic':True}
    finally:conn.close()


def test_windows_update_task_uses_standalone_entry_and_failure_restart():
    from knowbase.team.worker import windows_xml,NS
    from xml.etree import ElementTree as ET
    task=ET.fromstring(windows_xml('C:/fixed/python.exe','C:/cfg.json',900,entry_args=['-I','C:/runtime/updater.py','run'],execution_seconds=900,restart_on_failure=True))
    ns={'t':NS}
    assert task.find('.//t:Exec/t:Arguments',ns).text=='-I C:/runtime/updater.py run'
    assert task.find('.//t:ExecutionTimeLimit',ns).text=='PT900S'
    assert task.find('.//t:RestartOnFailure',ns) is not None
    assert task.find('.//t:RunLevel',ns).text=='LeastPrivilege'


def test_interrupted_recovery_precedes_network_backoff(update_world,monkeypatch):
    home,root,source,settings,installed=update_world;e=Engine(settings)
    atomic(e.journal,{'phase':'installing','worker_running':False})
    atomic(e.state_path,{'retry_at':time.time()+86400})
    visited=[]
    monkeypatch.setattr(e,'quiesce',lambda:None)
    def rollback(journal,temp):
        visited.append('recovered');atomic(e.journal,{'phase':'rolled_back'})
    monkeypatch.setattr(e,'rollback',rollback)
    monkeypatch.setattr(e,'fetch',lambda:pytest.fail('must recover without network'))
    assert e.run()['reason']=='interrupted_update_rolled_back'
    assert visited==['recovered']
