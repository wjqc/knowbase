"""Non-network/platform contracts for the remaining local capability gaps."""
import json
import plistlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from knowbase import config, index
from knowbase.automation import configuration, receipts
from knowbase.automation.protocol import DistillationRequest, ExecutorFailure
from knowbase.automation.text import IntranetTextRunner
from knowbase.value import aggregate, events, exchange_v2, outbox


def test_explicit_configuration_and_enable_require_receipt_scope_and_command_trust(memory_repo,tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    options={'executor_id':'fixture','command':sys.executable,'noninteractive_argv':['-c','pass']}
    p=tmp_path/'executor.json';p.write_text(json.dumps(options))
    assert configuration.configure(p)['verified'] is False
    with pytest.raises(ValueError):configuration.enable('fixture',['orders'],trust_command=True)
    receipts.record(options,{'passed':True})
    with pytest.raises(ValueError):configuration.enable('fixture',[])
    with pytest.raises(ValueError):configuration.enable('fixture',['orders'])
    assert configuration.enable('fixture',['orders'],trust_command=True)['enabled']
    assert configuration.disable()['queued_material_preserved']


def test_intranet_text_uses_explicit_chat_protocol_on_local_fixture_only():
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            seen.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            raw=json.dumps({'model':'fixture','choices':[{'message':{'content':'{"candidates":[],"segment_results":[]}'}}],
                            'usage':{'prompt_tokens':11,'completion_tokens':4}}).encode()
            self.send_response(200);self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        runner=IntranetTextRunner({'kind':'intranet_text','protocol':'openai_chat_completions',
            'endpoint':f'http://127.0.0.1:{server.server_port}/v1','model':'fixture',
            'allowed_hosts':['127.0.0.1'],'auth_kind':'none','allow_insecure_http':True})
        result=runner.run_text(DistillationRequest('fixture','orders','fixture'),'synthetic text',[])
        assert result['usage']['prompt_tokens']==11
        assert seen[0][0]=='/v1/chat/completions' and seen[0][1]['model']=='fixture'
        assert 'tools' not in seen[0][1]
    finally:server.shutdown();server.server_close();thread.join()


def test_persistent_scheduler_descriptors_do_not_kill_worker_after_five_minutes(memory_repo,tmp_path,monkeypatch):
    from test_team_worker import prepare
    from knowbase.team import worker
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'darwin')
    monkeypatch.setattr(worker.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    result=worker.install(persistent=True,argv=['worker','run'])
    doc=plistlib.loads(__import__('pathlib').Path(result['path']).read_bytes())
    assert doc['KeepAlive'] is True and 'StartInterval' not in doc
    task=ET.fromstring(worker.windows_xml(python,config.CONFIG_PATH,900,persistent=True))
    ns={'t':worker.NS}
    assert task.find('t:Settings/t:ExecutionTimeLimit',ns).text=='PT0S'
    assert task.find('t:Settings/t:RestartOnFailure/t:Count',ns).text=='3'


def test_persistent_linux_uses_service_not_oneshot_timer(memory_repo,tmp_path,monkeypatch):
    from test_team_worker import prepare
    from knowbase.team import worker
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'linux');calls=[]
    monkeypatch.setattr(worker.subprocess,'run',lambda args,**k:calls.append(args) or SimpleNamespace(returncode=0))
    worker.install(persistent=True,task_name='fixture-worker',argv=['worker','run'])
    service=(home/'.config/systemd/user/fixture-worker.service').read_text()
    assert 'Type=simple' in service and 'Restart=on-failure' in service
    assert 'TimeoutStartSec=300' not in service and not (home/'.config/systemd/user/fixture-worker.timer').exists()
    assert ['systemctl','--user','enable','--now','fixture-worker.service'] in calls


class MemoryGit:
    def __init__(self,package,receipt):
        self.data={'packages/'+package['package_id']+'.json':json.dumps(package),
                   'receipts/'+package['package_id']+'.json':json.dumps(receipt)}
    def fetch(self,*a):return 'fixture-revision'
    def files(self,*a):return list(self.data)
    def read(self,revision,path):return self.data.get(path)


def _fill(repo,n):
    c=index.connect(repo)
    for i in range(n):
        e=events.build_event('knowledge.delivered',{'delivery_id':f'del-{i}','memory_id':f'card-{i}',
            'content_revision':'r1','task_id':f'task-{i}'},host='fixture',session_id=f's-{i}')
        outbox.append(c,e)
    c.commit();c.close()


def test_large_outbox_makes_progress_and_acks_only_matching_package_team(memory_repo,tmp_path):
    cfg=config.load_config();cfg['value']['evidence_team_id']='company-fixture';config.CONFIG_PATH.write_text(json.dumps(cfg))
    _fill(memory_repo,1005);agg=aggregate.connect(tmp_path/'agg.db');total=0
    for _ in range(5):
        package=exchange_v2.export_events(memory_repo,states=('pending',))
        if not package['events']:break
        assert package['target_team']=='company-fixture' and len(package['events'])<=1000
        receipt=exchange_v2.import_package(agg,package,team_id='company-fixture')['receipt']
        wrong={**receipt,'team_id':'another-team'}
        assert exchange_v2.acknowledge_receipts(MemoryGit(package,wrong),memory_repo,branch='fixture')['acknowledged']==0
        total+=exchange_v2.acknowledge_receipts(MemoryGit(package,receipt),memory_repo,branch='fixture')['acknowledged']
    assert total==1005
    agg.close()


def test_receipt_cannot_omit_package_members(memory_repo,tmp_path):
    _fill(memory_repo,2);package=exchange_v2.export_events(memory_repo)
    agg=aggregate.connect(tmp_path/'agg.db');receipt=exchange_v2.import_package(agg,package)['receipt'];agg.close()
    receipt['events']=receipt['events'][:1]
    result=exchange_v2.acknowledge_receipts(MemoryGit(package,receipt),memory_repo,branch='fixture')
    assert result['acknowledged']==0 and result['mismatched']


def test_native_ocr_fallback_is_used_without_tesseract(tmp_path,monkeypatch):
    from PIL import Image
    from knowbase.parsers import ocr_parser,native_ocr
    p=tmp_path/'image.png';Image.new('RGB',(80,80),'white').save(p)
    monkeypatch.setattr(ocr_parser,'_check_tesseract_binary',lambda:None)
    monkeypatch.setattr(native_ocr,'available',lambda:True)
    monkeypatch.setattr(native_ocr,'recognize',lambda path:'synthetic OCR text')
    doc=ocr_parser.OcrParser().parse(p)
    assert doc.text=='synthetic OCR text' and doc.meta['engine']=='macos-vision'


def test_original_evidence_rebuild_is_separate_and_never_guesses(memory_repo,tmp_path):
    from knowbase.value.recovery import rebuild
    _fill(memory_repo,2)
    c=index.connect(memory_repo);before=c.execute('SELECT state,payload_hash FROM value_outbox ORDER BY event_id').fetchall();c.close()
    output=tmp_path/'recovered.db';result=rebuild(memory_repo,output)
    assert result['counts']['accepted']==2 and result['identity_still_missing']==2
    with pytest.raises(FileExistsError):rebuild(memory_repo,output)
    c=index.connect(memory_repo);assert c.execute('SELECT state,payload_hash FROM value_outbox ORDER BY event_id').fetchall()==before;c.close()


def test_init_configure_probe_enable_is_one_explicit_workflow(memory_repo,tmp_path,monkeypatch,capsys):
    from knowbase.__main__ import main
    from knowbase.automation import executor
    from test_p1_executor_probe import FAKE_GOOD_SCRIPT
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('KNOWBASE_TEST_AUTH','synthetic')
    monkeypatch.setattr(executor,'discover_executors',lambda:[])
    options={'executor_id':'fixture-init','command':sys.executable,'noninteractive_argv':['-c',FAKE_GOOD_SCRIPT],
             'auth_check':{'env_var':'KNOWBASE_TEST_AUTH'}}
    p=tmp_path/'executor.json';p.write_text(json.dumps(options))
    assert main(['init','--non-interactive','--executor-config',str(p),'--verify-executor','--scope','probe',
                 '--enable-automation','--executor-scopes','probe','--trust-executor-command'])==0
    cfg=config.load_config()
    assert cfg['automation']['enabled'] and cfg['automation']['allowed_scopes']==['probe']
    c=index.connect(memory_repo);assert c.execute('SELECT count(*) FROM meta').fetchone()[0]==0;c.close()


def test_worker_recognizes_batch_failure_lists():
    from knowbase.automation.worker import _outcome
    assert _outcome({'processed':1,'results':[{'error':'candidate_validation_failed'}]})==('error','candidate_validation_failed')


def test_windows_mcp_uses_same_fixed_scripts_runtime(tmp_path):
    from knowbase.integration import runtime_command
    assert runtime_command(tmp_path,platform='win32')==str(tmp_path/'.knowbase/runtime/venv/Scripts/python.exe')
