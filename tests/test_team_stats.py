"""Independent end-to-end counters, Git races and recovery boundaries."""
import copy
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase import config, index, telemetry
from knowbase.team import collect, publish, report, service, state
from knowbase.team.contracts import canonical, digest, stamp, summary_path, validate_summary
from knowbase.team.transport import GitData, TransportError, receive, send

NOW = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


def daily(device='device-a', member='alice', calls=10, *, known=20, hits=10, epoch='epoch-a', sequence=1):
    return {'schema':'knowbase.team-summary.v1','definition_version':'daily-observed-v1',
            'team_id':'team','member_id':member,'installation_id':device,'db_epoch':epoch,
            'snapshot_id':'snapshot-'+device,'source_timezone':'Asia/Shanghai',
            'period_start':'2026-10-22T16:00:00Z','period_end':'2026-10-23T16:00:00Z',
            'generated_at':stamp(NOW),'data_watermark':stamp(NOW),'sequence':sequence,
            'coverage':'partial','metrics':{'calls':calls,'known_searches':known,'hit_searches':hits,
            'missing_searches':0,'updates':0,'commands':[{'command':'memory_search','host':'zcode',
            'transport':'mcp','status':'ok','count':calls}],'usage':[]}}


def member(name, *, status='active', sequence=1):
    return {'schema':'knowbase.team-member.v1','team_id':'team','member_id':name,'label':name,
            'status':status,'joined_at':'2026-10-21T00:00:00Z','retired_at':None,
            'sequence':sequence,'confirmation':'self_confirmed' if status=='active' else 'pending'}


def accept(conn, v):
    path = f"members/{v['member_id']}.json" if 'label' in v else summary_path(v)
    return state.accept(conn, path, v, 'team')


@pytest.mark.parametrize('change', [lambda v: v['metrics'].update(calls=True),
    lambda v: v['metrics'].update(raw_query='PRIVATE_SENTINEL'),
    lambda v: v.update(installation_id='../x'),
    lambda v: v['metrics'].update(calls=11),
    lambda v: v.update(period_start='2026-10-22T00:00:00Z'),
    lambda v: v.update(coverage='complete'),
    lambda v: v['metrics']['commands'][0].update(raw_query='SECRET')])
def test_runtime_validation_rejects_invalid_or_private_payload(change):
    v=daily();change(v)
    with pytest.raises(ValueError):validate_summary(v)


def test_runtime_and_document_schema_agree():
    from jsonschema import Draft202012Validator, FormatChecker
    schema=json.loads((Path(__file__).parents[1]/'docs/schemas/team-summary.v1.schema.json').read_text())
    v=daily();validate_summary(v)
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(v)


def test_dynamic_members_weighted_hits_unknown_tokens_and_multiple_devices(tmp_path):
    c=state.connect(tmp_path)
    for n in ('alice','bob','carol'):accept(c, member(n))
    accept(c,daily())
    accept(c,daily('device-b','bob',20,known=1,hits=1))
    out=report.build(c,now=NOW,inventory={'total':1,'states':{'verified':1},'revision':'abc'})
    assert out['calls']==30 and out['knowledge_total']==1
    assert out['hit_rate']==52.4  # 11/21, never average member percentages
    assert out['coverage']['received']==2 and out['coverage']['registered']==3
    assert out['usage']['total'] is None
    assert next(m for m in out['members'] if m['member_id']=='carol')['totals']['calls'] is None
    # A second device belongs to the same person, not another member.
    accept(c,daily('device-a2','alice',4))
    assert report.build(c,now=NOW)['calls']==34
    for i in range(12):accept(c,member('new'+str(i),status='pending'))
    assert report.build(c,now=NOW)['coverage']['registered']==15
    retired=member('bob',status='retired',sequence=2)
    retired.update(confirmation='publisher_confirmed',retired_at='2026-10-23T08:00:00Z')
    accept(c,retired)
    out=report.build(c,now=NOW)
    assert out['coverage']['registered']==14 and out['calls']==34
    # Historical roster at the end of an earlier day does not inherit today's departure.
    historic=report.build(c,now=NOW,until=datetime(2026,10,22,16,tzinfo=timezone.utc))
    assert next(m for m in historic['members'] if m['member_id']=='bob')['current']
    c.close()


def test_replay_stale_sequence_and_epoch_overlap_keep_confirmed_lower_bound(tmp_path):
    c=state.connect(tmp_path);accept(c,member('alice'));v=daily()
    assert accept(c,v)
    for _ in range(10):assert not accept(c,v)
    newer=copy.deepcopy(v);newer['sequence']=3;newer['metrics']['calls']=12;newer['metrics']['commands'][0]['count']=12
    accept(c,newer);assert not accept(c,v)
    bad=copy.deepcopy(newer);bad['sequence']=4;bad['metrics']['calls']=1;bad['metrics']['commands'][0]['count']=1
    with pytest.raises(ValueError):accept(c,bad)
    assert report.build(c,now=NOW)['calls']==12
    reset=daily(calls=3,epoch='epoch-reset');reset['generated_at']='2026-10-23T10:00:00Z'
    accept(c,reset)
    out=report.build(c,now=NOW)
    assert out['calls']==12 and out['coverage']['recovery_members']==1
    assert out['members'][0]['coverage']=='recovery_required'
    c.close()


def test_collector_filters_real_day_windows_and_detects_log_loss(memory_repo,tmp_path):
    c=state.connect(tmp_path/'state')
    db=index.connect(memory_repo)
    from knowbase.value import identity
    install_id=identity.install(db);db.commit()
    e={'team_id':'team','member_id':'alice','installation_id':install_id,'source_timezone':'Asia/Shanghai',
       'enrolled_at':'2026-10-20T00:00:00Z','include_history':True,'historical_ids':[]}
    state.put(c,'enrollment',e);state.put(c,'epoch','epoch-a')
    for i, ts in enumerate(('2026-10-22T15:59:59Z','2026-10-22T16:00:00Z','2026-10-23T09:00:00Z','2026-10-23T16:00:00Z')):
        db.execute('INSERT INTO command_calls VALUES(?,?,?,?,?,?,?)',(str(i),ts,'mcp','memory_search','ok',0,install_id))
        db.execute('INSERT INTO command_call_hosts VALUES(?,?)',(str(i),'zcode'))
    for ts,detail in [('2026-10-22T23:59:00',{'hits':[]}),('2026-10-23T00:00:00',{'hits':[1]}),('2026-10-23T02:00:00',{})]:
        db.execute('INSERT INTO usage_log VALUES(?,?,?,?,?)',(ts,'zcode','search','',json.dumps(detail)))
    db.commit()
    usages=[{'request_id':'r1','occurred_at':'2026-10-23T00:00:00Z','model':'claude-example','input_tokens':20,'output_tokens':None,'cache_read_tokens':0,'cache_creation_tokens':0}]
    collect.collect(memory_repo,c,now=NOW,usage_records=usages)
    vals=[json.loads(r[0]) for r in c.execute('SELECT body FROM outgoing')]
    by_start={v['period_start']:v for v in vals}
    assert by_start['2026-10-22T16:00:00Z']['metrics']['calls']==2
    assert by_start['2026-10-21T16:00:00Z']['metrics']['calls']==1
    today=by_start['2026-10-22T16:00:00Z']
    assert today['metrics']['known_searches']==1 and today['metrics']['hit_searches']==1 and today['metrics']['missing_searches']==1
    assert today['metrics']['usage'][0]['output_tokens'] is None
    # same observation does not create another outgoing generation inside the heartbeat.
    assert collect.collect(memory_repo,c,now=NOW+timedelta(minutes=15),usage_records=usages)==0
    db.execute("DELETE FROM command_calls WHERE call_id='1'");db.commit()
    collect.collect(memory_repo,c,now=NOW+timedelta(minutes=30),usage_records=usages)
    assert '2026-10-23' in state.get(c,'recovery_days')
    assert state.get(c,'epoch')!='epoch-a'
    # raw log/search content is never exported.
    assert 'hits' not in ''.join(r[0] for r in c.execute('SELECT body FROM outgoing'))
    db.close();c.close()


def gitrun(path,*args):
    return subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True,text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    bare=tmp_path/'remote.git';subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
    seed=tmp_path/'seed';seed.mkdir();gitrun(seed,'init','-b','main')
    gitrun(seed,'config','user.name','test');gitrun(seed,'config','user.email','test@example.invalid')
    (seed/'README.md').write_text('# Shared knowledge\n\nHuman introduction.\n')
    (seed/'pitfalls').mkdir();(seed/'pitfalls/card.md').write_text('---\nid: P-2026-0001\nconfidence: verified\nstatus: active\n---\nPRIVATE_CARD_BODY\n')
    gitrun(seed,'add','.');gitrun(seed,'commit','-m','seed');gitrun(seed,'remote','add','origin',bare.as_uri());gitrun(seed,'push','origin','main')
    return bare.as_uri(),seed


def device(tmp_path,remote,name,number):
    root=tmp_path/name;c=state.connect(root)
    g=GitData(root/'git',remote,[remote])
    m=member(name);v=daily('device-'+name,name,number)
    state.queue(c,f'members/{name}.json',m);state.queue(c,summary_path(v),v);c.commit()
    return g,c


def test_three_device_push_race_idempotent_replay_offline_recovery(tmp_path,remote,monkeypatch):
    url,seed=remote
    a,ca=device(tmp_path,url,'alice',10);b,cb=device(tmp_path,url,'bob',20);d,cd=device(tmp_path,url,'carol',30)
    original=a.push; raced=False
    def racing_push(branch,commit):
        nonlocal raced
        if not raced:raced=True;send(b,cb,'team')
        return original(branch,commit)
    monkeypatch.setattr(a,'push',racing_push)
    send(a,ca,'team');send(d,cd,'team');receive(a,ca,'team')
    out=report.build(ca,now=NOW)
    assert out['calls']==60 and out['coverage']['received']==3 and out['coverage']['registered']==3
    send(a,ca,'team');receive(a,ca,'team')
    assert report.build(ca,now=NOW)['calls']==60
    assert ca.execute('SELECT COUNT(*) FROM outgoing WHERE hash<>ack').fetchone()[0]==0
    # A new day revision remains pending while disconnected, then is acked on reconnect.
    v=daily('device-alice','alice',11,sequence=2);state.queue(ca,summary_path(v),v);ca.commit()
    original_remote=a.remote;a.remote=original_remote+'-offline'
    with pytest.raises(TransportError):send(a,ca,'team')
    assert ca.execute('SELECT COUNT(*) FROM outgoing WHERE hash<>ack').fetchone()[0]==1
    a.remote=original_remote;send(a,ca,'team');receive(a,ca,'team')
    assert report.build(ca,now=NOW)['calls']==61
    # Knowledge checkout was never switched, rebased, reset or changed.
    assert gitrun(seed,'status','--porcelain')=='' and gitrun(seed,'branch','--show-current')=='main'
    for c in (ca,cb,cd):c.close()


def test_readme_single_region_preserves_human_text_inventory_and_manual_conflict(tmp_path,remote):
    url,seed=remote;g,c=device(tmp_path,url,'alice',10)
    send(g,c,'team');receive(g,c,'team')
    rgit=GitData(tmp_path/'readme',url,[url])
    rev=rgit.fetch('main');inv=publish.inventory(rgit,rev)
    assert inv['total']==1
    snap=report.build(c,now=NOW,inventory=inv)
    publish.publish(rgit,'main',snap)
    rev=rgit.fetch('main');readme=rgit.read(rev,'README.md')
    assert 'Human introduction.' in readme and '10' in readme and 'PRIVATE_CARD_BODY' not in readme
    assert readme.count(publish.START)==1
    # Separate normal knowledge commits and publisher updates converge.
    gitrun(seed,'pull','--ff-only','origin','main')
    (seed/'README.md').write_text(readme+'\nHuman footer.\n');gitrun(seed,'add','README.md');gitrun(seed,'commit','-m','human');gitrun(seed,'push','origin','main')
    snap['generated_at']='2026-10-23T10:00:00Z';publish.publish(rgit,'main',snap)
    assert 'Human footer.' in rgit.read(rgit.fetch('main'),'README.md')
    gitrun(seed,'pull','--ff-only','origin','main')
    text=(seed/'README.md').read_text().replace('团队知识库使用统计','Manually changed')
    (seed/'README.md').write_text(text);gitrun(seed,'add','README.md');gitrun(seed,'commit','-m','edit region');gitrun(seed,'push','origin','main')
    with pytest.raises(TransportError,match='edited'):publish.publish(rgit,'main',snap)
    assert 'Manually changed' in rgit.read(rgit.fetch('main'),'README.md')
    c.close()


def test_malformed_remote_private_fields_are_rejected_not_rendered(tmp_path,remote):
    url,_=remote;g,c=device(tmp_path,url,'alice',10)
    send(g,c,'team')
    v=daily('bad','alice',1);v['raw_prompt']='PRIVATE_SENTINEL'
    rev=g.fetch('knowbase-stats');commit=g.commit(rev,{'devices/bad/epoch-a/2026-10-23.json':canonical(v).decode()})
    g.push('knowbase-stats',commit);receive(g,c,'team')
    assert state.get(c,'rejected_files')==1
    assert 'PRIVATE_SENTINEL' not in canonical(report.build(c,now=NOW)).decode()
    c.close()


def test_pre_registered_member_self_enrollment_promotes_registry(tmp_path,remote):
    url,_=remote;g,c=device(tmp_path,url,'alice',10)
    state.queue(c,'members/bob.json',member('bob',status='pending',sequence=3));c.commit()
    send(g,c,'team')
    gb,cb=device(tmp_path,url,'bob',20)
    send(gb,cb,'team');receive(g, c,'team')
    bob=json.loads(c.execute("SELECT body FROM accepted WHERE path='members/bob.json'").fetchone()[0])
    assert bob['status']=='active' and bob['sequence']==4 and bob['confirmation']=='self_confirmed'
    assert report.build(c,now=NOW)['coverage']['received']==2
    c.close();cb.close()


def test_new_receiver_does_not_choose_between_overlapping_epochs(tmp_path):
    c=state.connect(tmp_path);accept(c,member('alice'))
    accept(c,daily());accept(c,daily(calls=12,epoch='other'))
    out=report.build(c,now=NOW)
    assert out['calls'] is None and out['coverage']['recovery_members']==1
    c.close()


def test_telemetry_records_real_host_without_inventing_legacy_host(memory_repo,monkeypatch):
    monkeypatch.setenv('KNOWBASE_AGENT_NAME','claude')
    telemetry.record('mcp','memory_search','ok',1)
    telemetry.record('cli','hook.tool-before','ok',1)
    telemetry.record('cli','stats','ok',1)
    db=index.connect(memory_repo)
    values=dict(db.execute('SELECT c.command,h.host FROM command_calls c JOIN command_call_hosts h ON h.call_id=c.call_id'))
    assert values=={'memory_search':'claude-code','hook.tool-before':'claude-code','stats':'cli'}
    db.close()


def test_service_enrollment_sync_and_idle_no_knowledge_commit(memory_repo,tmp_path,remote):
    url,seed=remote
    cfg=config.load_config()
    cfg['git']['remote'].update(url=url,branch='main')
    cfg['git']['allowed_remote_prefixes']=[url]
    cfg['team'].update(data_dir=str(tmp_path/'team-data'),dashboard_output=str(tmp_path/'dashboard.html'))
    cfg['dashboard']['host_usage_source']='none'
    service._save_config(cfg)
    result=service.enroll('alice','alice',publisher=True)
    assert result['enrolled'] and result['pending_files']==1
    for _ in range(10):telemetry.record('mcp','memory_search','ok',1)
    result=service.run()
    assert result['errors']==[] and result['coverage']['received']==1 and result['pending_files']==0
    snap=json.loads((tmp_path/'team-data/snapshot.json').read_text())
    assert snap['calls']==10 and snap['knowledge_total']==1
    assert len(list((tmp_path/'team-data/backups').glob('*/memory.db')))==1
    readme_git=GitData(tmp_path/'verify',url,[url]);first=readme_git.fetch('main')
    # No new calls and no heartbeat due: no new default-branch commit.
    second=service.run();assert second['errors']==[]
    assert readme_git.fetch('main')==first
    assert (tmp_path/'dashboard.html').exists()
    # Retirement keeps the old member's observed totals.
    service.member('bob','bob','pending');service.run()
    snap=json.loads((tmp_path/'team-data/snapshot.json').read_text())
    assert snap['coverage']['registered']==2 and snap['coverage']['received']==1
    assert gitrun(seed,'status','--porcelain')==''


def test_copied_team_state_cannot_run_as_same_device(memory_repo,tmp_path,remote,monkeypatch):
    url,_=remote;cfg=config.load_config()
    cfg['git']['remote'].update(url=url,branch='main');cfg['git']['allowed_remote_prefixes']=[url]
    cfg['team'].update(data_dir=str(tmp_path/'team-data'))
    service._save_config(cfg);service.enroll('alice','alice')
    monkeypatch.setattr(service,'device_fingerprint',lambda:'different-machine')
    with pytest.raises(ValueError,match='another computer'):service.run()


def test_launchagent_fixed_runtime_and_removal(memory_repo,tmp_path,monkeypatch):
    from knowbase.team import worker
    import plistlib
    from types import SimpleNamespace
    home=tmp_path/'home';python=home/'.knowbase/runtime/venv/bin/python'
    python.parent.mkdir(parents=True);python.touch()
    monkeypatch.setattr(worker.Path,'home',lambda:home)
    monkeypatch.setattr(worker.sys,'platform','darwin')
    calls=[]
    def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess,'run',run)
    cfg=config.load_config();cfg['team'].update(data_dir=str(tmp_path/'data'))
    service._save_config(cfg)
    result=worker.install();p=Path(result['path'])
    value=plistlib.loads(p.read_bytes())
    assert value['ProgramArguments']==[str(python),'-I','-m','knowbase','team','sync']
    assert value['StartInterval']==900 and value['RunAtLoad']
    assert any('bootstrap' in a for a in calls)
    worker.install(remove=True);assert not p.exists()


def test_offline_first_enrollment_roster_is_explicitly_pending(tmp_path):
    c=state.connect(tmp_path)
    for name in ('alice','bob','carol'):state.queue(c,f'members/{name}.json',member(name))
    out=report.build(c,now=NOW)
    assert out['coverage']['registered']==3 and out['coverage']['received']==0
    assert out['coverage']['pending_registry']==3 and out['calls'] is None
    c.close()


def test_member_retirement_uses_latest_remote_activation(memory_repo,tmp_path):
    cfg=config.load_config();cfg['team']['data_dir']=str(tmp_path/'team-data');service._save_config(cfg)
    c=state.connect(tmp_path/'team-data')
    state.put(c,'enrollment',{'team_id':'team','member_id':'alice','publisher':True})
    state.queue(c,'members/bob.json',member('bob',status='pending',sequence=1))
    accept(c,member('bob',sequence=2));c.commit();c.close()
    service.member('bob','bob','retired')
    c=state.connect(tmp_path/'team-data')
    v=json.loads(c.execute("SELECT body FROM outgoing WHERE path='members/bob.json'").fetchone()[0])
    assert v['sequence']==3 and v['status']=='retired'
    c.close()


def test_schema_upgrade_keeps_old_running_process_positional_insert(memory_repo):
    db=index.connect(memory_repo)
    # The already-loaded rc7 telemetry writes seven positional values.
    db.execute('INSERT INTO command_calls VALUES(?,?,?,?,?,?,?)',('old-process',stamp(),'mcp','memory_search','ok',0,''))
    db.commit()
    row=db.execute("SELECT c.call_id,COALESCE(h.host,'unknown') FROM command_calls c LEFT JOIN command_call_hosts h ON c.call_id=h.call_id WHERE c.call_id='old-process'").fetchone()
    assert tuple(row)==('old-process','unknown')
    db.close()


def test_sender_cannot_update_another_devices_path(tmp_path,remote):
    url,_=remote;g,c=device(tmp_path,url,'alice',10)
    state.put(c,'enrollment',{'member_id':'alice','installation_id':'owned-device','publisher':False})
    with pytest.raises(TransportError,match='ownership'):send(g,c,'team')
    assert c.execute('SELECT COUNT(*) FROM outgoing WHERE hash<>ack').fetchone()[0]==2
    c.close()


def test_old_connection_identity_counts_only_after_enrollment_without_backfill(memory_repo,tmp_path):
    from knowbase.value import identity
    db=index.connect(memory_repo);current=identity.install(db);db.commit()
    c=state.connect(tmp_path/'team-data')
    state.put(c,'enrollment',{'team_id':'team','member_id':'alice','installation_id':current,
        'source_timezone':'Asia/Shanghai','enrolled_at':'2026-10-23T01:00:00Z','include_history':False,'historical_ids':['old-connection']})
    state.put(c,'epoch','epoch-a')
    for key,ts in [('before','2026-10-23T00:59:59Z'),('after','2026-10-23T01:00:01Z')]:
        db.execute('INSERT INTO command_calls VALUES(?,?,?,?,?,?,?)',(key,ts,'mcp','memory_search','ok',0,'old-connection'))
    db.commit();collect.collect(memory_repo,c,now=NOW)
    summaries=[json.loads(r[0]) for r in c.execute('SELECT body FROM outgoing')]
    assert sum(v['metrics']['calls'] or 0 for v in summaries)==1
    db.close();c.close()


def test_stable_device_binding_ignores_hostname_and_network(monkeypatch):
    monkeypatch.setattr(service,'_machine_identifier',lambda:'a'*32)
    before=service.device_fingerprint()
    monkeypatch.setattr(service.platform,'node',lambda:'new-dhcp-name')
    monkeypatch.setattr(service.uuid,'getnode',lambda:123456789)
    assert service.device_fingerprint()==before
    monkeypatch.setattr(service,'_machine_identifier',lambda:'b'*32)
    assert service.device_fingerprint()!=before


def test_explicit_rebind_preserves_evidence_and_rejects_wrong_database(memory_repo,tmp_path,remote,monkeypatch):
    url,_=remote;cfg=config.load_config()
    cfg['git']['remote'].update(url=url,branch='main');cfg['git']['allowed_remote_prefixes']=[url]
    root=tmp_path/'team-data';cfg['team']['data_dir']=str(root);service._save_config(cfg)
    service.enroll('alice','alice')
    c=state.connect(root);old=state.get(c,'enrollment');epoch=state.get(c,'epoch')
    queued=c.execute('SELECT * FROM outgoing').fetchall();c.close()
    monkeypatch.setattr(service,'device_fingerprint',lambda:'stable-new-fingerprint')
    with pytest.raises(ValueError,match='another computer'):service.run(network=False)
    assert service.rebind_device()['identity_preserved']
    c=state.connect(root);new=state.get(c,'enrollment')
    assert {k:v for k,v in new.items() if k!='device_fingerprint'}=={k:v for k,v in old.items() if k!='device_fingerprint'}
    assert state.get(c,'epoch')==epoch and c.execute('SELECT * FROM outgoing').fetchall()==queued
    assert list((root/'backups').glob('device-rebind-*/state.db'))
    c.close();assert service.rebind_device()=={'device_binding':'unchanged'}
    db=index.connect(memory_repo);identity=__import__('knowbase.value.identity',fromlist=['_put'])
    identity._put(db,'installation_id','another-installation');db.commit();db.close()
    with pytest.raises(ValueError,match='database identity mismatch'):service.rebind_device()


def _email_gate(bare):
    """Simulate a corporate AIQGATE-style pre-receive e-mail domain rule."""
    hook = bare / 'hooks' / 'pre-receive'
    hook.write_text(
        '#!/bin/sh\n'
        'zero=0000000000000000000000000000000000000000\n'
        'while read old new ref; do\n'
        '  [ "$new" = "$zero" ] && continue\n'
        '  email=$(git log -1 --format=%ce "$new")\n'
        '  case "$email" in\n'
        '    *@asiainfo.com) ;;\n'
        '    *) echo "Email domain should be asiainfo.com. Your current email is $email" >&2; exit 1;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n')
    hook.chmod(0o755)


def test_gateway_email_domain_gate_requires_real_committer_identity(tmp_path, remote):
    url, _ = remote
    _email_gate(tmp_path / 'remote.git')
    # The synthetic default identity is declined; pending statistics stay retained.
    g0, c0 = device(tmp_path / 'default', url, 'dave', 5)
    with pytest.raises(TransportError):
        send(g0, c0, 'team')
    # Both the member registration and the daily summary stay pending.
    assert c0.execute('SELECT COUNT(*) FROM outgoing WHERE hash<>ack').fetchone()[0] == 2
    # A corporate identity passes the same gate and is confirmed on read-back.
    g1, c1 = device(tmp_path / 'real', url, 'erin', 7)
    g1.identity = ('erin', 'erin@asiainfo.com')
    send(g1, c1, 'team')
    receive(g1, c1, 'team')
    assert c1.execute('SELECT COUNT(*) FROM outgoing WHERE hash<>ack').fetchone()[0] == 0
    assert g1.run('log', '-1', '--format=%ce', 'refs/remotes/team/knowbase-stats') == 'erin@asiainfo.com'
    assert report.build(c1, now=NOW)['coverage']['received'] == 1
    for c in (c0, c1):
        c.close()


def test_commit_identity_resolution_order(memory_repo, monkeypatch):
    cfg = config.load_config()
    cfg['team'].update(commit_name='ops', commit_email='ops@asiainfo.com')
    assert service.commit_identity(cfg) == ('ops', 'ops@asiainfo.com')
    def fake_run_git(args, timeout):
        return (0, {'user.name': ' ambient-user\n', 'user.email': 'ambient@asiainfo.com\n'}[args[-1]], '')
    monkeypatch.setattr(service.gitops, '_run_git', fake_run_git)
    # Per-field fallback: explicit e-mail keeps the machine's own name.
    cfg['team'].update(commit_name='', commit_email='ops@asiainfo.com')
    assert service.commit_identity(cfg) == ('ambient-user', 'ops@asiainfo.com')
    # Empty config falls back to the ambient Git identity entirely.
    cfg['team'].update(commit_name='', commit_email='')
    assert service.commit_identity(cfg) == ('ambient-user', 'ambient@asiainfo.com')
    # No config and no Git identity keeps the synthetic default.
    monkeypatch.setattr(service.gitops, '_run_git', lambda args, timeout: (1, '', ''))
    assert service.commit_identity(cfg) == GitData.DEFAULT_IDENTITY


def test_service_pushes_statistics_through_email_domain_gate(memory_repo, tmp_path, remote):
    url, _ = remote
    _email_gate(tmp_path / 'remote.git')
    cfg = config.load_config()
    cfg['git']['remote'].update(url=url, branch='main')
    cfg['git']['allowed_remote_prefixes'] = [url]
    cfg['team'].update(data_dir=str(tmp_path / 'team-data'), dashboard_output=str(tmp_path / 'dashboard.html'),
                       commit_name='alice', commit_email='alice@asiainfo.com')
    cfg['dashboard']['host_usage_source'] = 'none'
    service._save_config(cfg)
    assert service.enroll('alice', 'alice', publisher=True)['enrolled']
    for _ in range(3):
        telemetry.record('mcp', 'memory_search', 'ok', 1)
    result = service.run()
    assert result['errors'] == [] and result['coverage']['received'] == 1 and result['pending_files'] == 0
    snap = json.loads((tmp_path / 'team-data' / 'snapshot.json').read_text())
    assert snap['calls'] == 3
