"""Explicit bootstrap/enrollment of the independent program-update channel."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .. import config, integration
from .engine import Engine, UpdateError, atomic, read

LABEL='com.knowbase.updater'
TASK='knowbase-updater'


from ..processes import no_window

def settings_path():return Path.home()/'.knowbase/updates.json'


def existing_agents(home):
    result=[]
    for name in integration.HOSTS:
        spec=integration.host_spec(name,home)
        try:
            doc=integration._document(integration._read(home/spec['mcp']))
            server=integration._section(doc,spec['key']).get('knowbase',{})
            if integration.legacy_serve_args(server.get('args'),home):result.append(name)
        except (ValueError,OSError):pass
    for name in ['codex','opencode']:
        if any(h['id']==name and h.get('configured') for h in integration.discover(home)):result.append(name)
    return result


def runtime_files(home):
    root=home/'.knowbase/runtime';source=Path(__file__).parent
    mapping={'updater.py':'engine.py','mcp_bridge.py':'bridge.py','entry.py':'entry.py'}
    existing=read(root/'managed-update.json');hashes={}
    for target,name in mapping.items():
        path=root/target;raw=(source/name).read_bytes()
        if path.is_symlink():raise UpdateError('unsafe_updater_entry')
        if path.exists() and target not in existing.get('files',{}) and path.read_bytes()!=raw:raise UpdateError('unmanaged_updater_entry')
        compile(raw,str(path),'exec')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        temp=path.with_name(path.name+'.'+uuid.uuid4().hex);temp.write_bytes(raw);temp.chmod(0o600);os.replace(temp,path)
        hashes[target]=hashlib.sha256(raw).hexdigest()
    atomic(root/'managed-update.json',{'schema':'knowbase.update-entries.v1','files':hashes})


def install_schedule(*, remove=False):
    from ..team import worker
    cfg=read(settings_path());home=Path(cfg.get('home',str(Path.home()))).resolve()
    return worker.install(remove=remove,label=LABEL,task_name=TASK,
        description='knowbase unattended program updater',descriptor_name='knowbase-updater-task.xml',
        entry_args=['-I',str(home/'.knowbase/runtime/updater.py'),'run','--settings',str(settings_path().resolve())],
        interval_seconds=int(cfg.get('interval_seconds',900)),execution_seconds=900,restart_on_failure=True)


def enable(source, *, agents=None, branch='main', interval=900, allow_insecure=False, allow_file=False):
    home=Path.home().resolve();path=settings_path();old=path.read_bytes() if path.exists() else None
    settings={'schema':'knowbase.updates.v1','enabled':True,'home':str(home),
        'runtime_python':integration.runtime_command(home),'source_url':source,'branch':branch,
        'interval_seconds':max(300,min(86400,int(interval))),'max_wait_seconds':60,
        'uv_path':shutil.which('uv'),'config_path':str(config.CONFIG_PATH.resolve()),
        'allow_insecure_source':bool(allow_insecure),'allow_file_source':bool(allow_file)}
    # Validate before touching host profiles or scheduler. Source access occurs on the timer.
    test=path.with_name('.updates-'+uuid.uuid4().hex+'.json');atomic(test,settings)
    try:Engine(test)
    finally:test.unlink(missing_ok=True)
    chosen=agents if agents is not None else existing_agents(home)
    runtime_files(home)
    changes=integration.plan(chosen,home,managed=True)
    changes.append({'path':str(path.resolve()),'before':old,'after':(json.dumps(settings,ensure_ascii=False,indent=2)+'\n').encode()})
    marker=home/'.knowbase/updates/source.json'
    if marker.exists() and read(marker).get('source_url')!=source:
        # Explicit enable --source authorizes moving the source endpoint; keep Git
        # history for rollback and still require a descendant of the installed commit.
        changes.append({'path':str(marker),'before':marker.read_bytes(),
                        'after':(json.dumps({'source_url':source})+'\n').encode()})
    installed=integration.apply(changes,home,allowed_paths=(path.resolve(),))
    try:schedule=install_schedule()
    except Exception:
        if installed.get('backup'):integration.recover(Path(installed['backup'])/'journal.json')
        raise
    return {'enabled':True,'source_url':source,'branch':branch,'interval_seconds':settings['interval_seconds'],
            'agents':chosen,'scheduler':schedule,'first_reconnect_required':True,
            'other_computers':'initial_bootstrap_required','version_policy':'single in-place runtime'}


def disable():
    cfg=read(settings_path());directory=Path(cfg.get('home',str(Path.home())))/'.knowbase/updates'
    from .engine import Lock
    with Lock(directory/'.lock'):
        if read(directory/'journal.json').get('phase') in {'installing','migrating'}:raise UpdateError('update_recovery_in_progress')
        cfg['enabled']=False;atomic(settings_path(),cfg);result=install_schedule(remove=True)
    return {'enabled':False,'scheduler':result,'knowledge_and_membership_preserved':True}


def status():
    cfg=read(settings_path());home=Path(cfg.get('home',str(Path.home())))
    return {'enabled':bool(cfg.get('enabled')),'source_url':cfg.get('source_url'),
            'branch':cfg.get('branch'),'interval_seconds':cfg.get('interval_seconds'),
            'last_run':read(home/'.knowbase/updates/state.json'),
            'draining':(home/'.knowbase/runtime/update-drain.json').exists(),
            'journal_phase':read(home/'.knowbase/updates/journal.json').get('phase')}


def run(*, force=False, action='run'):
    cfg=read(settings_path());home=Path(cfg.get('home',str(Path.home())))
    args=[integration.runtime_command(home),'-I',str(home/'.knowbase/runtime/updater.py'),action,
          '--settings',str(settings_path().resolve())]
    if force:args.append('--force')
    # The child survives replacement of knowbase. No in-process package mutation.
    result=subprocess.run(args,capture_output=True,text=True,timeout=900, **no_window())
    if result.returncode:raise UpdateError('independent_updater_failed')
    return json.loads(result.stdout)
