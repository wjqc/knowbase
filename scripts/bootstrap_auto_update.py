#!/usr/bin/env python3
"""One-time enrollment of an old computer. Future updates need no manual pull/install.

Run with the computer's existing Python >=3.10, Git and uv. This installer
does not reuse the knowledge repository as executable code.
"""
import argparse
import email
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def command(args,**kwargs):
    result=subprocess.run([str(x) for x in args],capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never',**kwargs.pop('env',{})},**kwargs)
    if result.returncode:raise RuntimeError('bootstrap step failed; existing backup/configuration retained')
    return result.stdout


def main(engine_module=None):
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--branch',default='main')
    parser.add_argument('--agents',default='');parser.add_argument('--member-id');parser.add_argument('--label');parser.add_argument('--publisher',action='store_true')
    parser.add_argument('--allow-insecure-source',action='store_true');args=parser.parse_args()
    if sys.version_info<(3,10):raise RuntimeError('Python >=3.10 required')
    uv=shutil.which('uv')
    if not uv or not shutil.which('git'):raise RuntimeError('Git and uv must be installed once')
    home=Path.home();root=home/'.knowbase/runtime';python=root/('venv/Scripts/python.exe' if os.name=='nt' else 'venv/bin/python')
    # Reuse the audited standalone engine from this distribution; it imports no app code.
    engine_path=Path(__file__).resolve().parents[1]/'knowbase/updates/engine.py'
    if not engine_path.exists():raise RuntimeError('run this bootstrap from a trusted knowbase source checkout')
    if engine_module is None:
        spec=importlib.util.spec_from_file_location('knowbase_bootstrap_engine',engine_path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    else:module=engine_module  # Allows isolated scheduler substitution in installer integration tests.
    cp=home/'.knowbase/config.json'
    with tempfile.TemporaryDirectory(prefix='knowbase-bootstrap-') as td:
        temp=Path(td);settings=temp/'settings.json'
        module.atomic(settings,{'home':str(home),'enabled':True,'runtime_python':str(python),'config_path':str(cp),
            'source_url':args.source,'branch':args.branch,'uv_path':uv,'allow_insecure_source':args.allow_insecure_source})
        engine=module.Engine(settings);target=engine.fetch();source=engine.export(target,temp/'source')
        if not (source/'knowbase/updates/engine.py').is_file() or not (source/'pyproject.toml').is_file():
            raise RuntimeError('program source required, not the knowledge repository')
        # Build-time dependencies may be downloaded only during this explicit first enrollment.
        command([uv,'build','--wheel','--out-dir',temp/'wheel',source],timeout=300)
        wheel=next((temp/'wheel').glob('knowbase-*.whl'));candidate=engine.unpack(wheel,temp/'candidate')
        if not python.exists():command([uv,'venv','--python',sys.executable,root/'venv'],timeout=180)
        with module.Lock(engine.directory/'.lock'):
            managed={b.get('backend_pid') for b in engine.live_bridges()}
            for pid,cmd in engine.processes():
                runtime_args=engine.runtime_args(cmd)
                if pid not in managed and runtime_args is not None and __import__('re').search(r'(?:^|\s)-m\s+knowbase\s+serve(?:\s|$)',cmd):
                    raise RuntimeError('first enrollment: close/reconnect the legacy knowbase MCP once, then rerun')
            engine.quiesce();running=engine.worker_running();engine.worker(False)
            backup=None;old_wheel=None;profiles_started=False
            previous_journals={str(p.resolve()) for p in (home/'.knowbase/backups').glob('init-*/journal.json')}
            try:
                if cp.exists():backup=engine.backup()
                # Snapshot through the fixed interpreter, not whichever Python launched this script.
                if (root/'venv').exists():
                    code="import sys,importlib.util;s=importlib.util.spec_from_file_location('snapshot_engine',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.snapshot_wheel(sys.argv[2]) or '')"
                    raw=command([python,'-I','-c',code,engine_path,temp],timeout=30).decode().strip()
                    old_wheel=Path(raw) if raw else None
                metadata=email.message_from_bytes(next(candidate.glob('*.dist-info/METADATA')).read_bytes())
                requirements=temp/'requirements.txt';requirements.write_text('\n'.join(x for x in metadata.get_all('Requires-Dist',[]) if 'extra ==' not in x)+'\n')
                command([uv,'pip','install','--python',python,'-r',requirements],timeout=300)
                validation="import sys,importlib.util;from pathlib import Path;s=importlib.util.spec_from_file_location('bootstrap_validation',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.Engine(sys.argv[2]).source_info(Path(sys.argv[3]))"
                command([python,'-I','-c',validation,engine_path,settings,source],timeout=30)
                if cp.exists():
                    # Shadow validation executes with dependencies from the target interpreter.
                    code="import sys;sys.path.insert(0,sys.argv.pop(1));from knowbase.updates.engine import Engine;from pathlib import Path;e=Engine(sys.argv[1]);e.source_info(Path(sys.argv[2]));e.shadow(Path(sys.argv[3]),Path(sys.argv[4]))"
                    (temp/'shadow').mkdir()
                    command([python,'-I','-c',code,str(Path(__file__).resolve().parents[1]),settings,source,candidate,temp/'shadow'],timeout=120)
                command([uv,'pip','install','--python',python,'--no-deps','--reinstall',wheel],timeout=180)
                init=[python,'-I','-m','knowbase','init','--non-interactive','--auto-update','--update-source',args.source,'--update-branch',args.branch]
                if args.agents:init+=['--agents',args.agents]
                if args.allow_insecure_source:init+=['--allow-insecure-update-source']
                profiles_started=True
                command(init,env={'KNOWBASE_MANAGED_UPDATE':'1'},timeout=120)
                engine.app=module.read(cp);engine.repo=Path(os.path.expanduser(engine.app['repo_path'])).resolve()
                engine.probe(Path(engine.git_package_path()),cp)
                if backup and not module.ledger_unchanged(engine.repo/'memory.db',module.ledger_snapshot(backup/'memory.db')):
                    before=module.ledger_snapshot(backup/'memory.db');after=module.ledger_snapshot(engine.repo/'memory.db',before)
                    changed=','.join(k for k,v in before.items() if after.get(k)!=v)
                    raise RuntimeError('user ledger changed ('+changed+'); inspect preserved backup before any restoration')
                info=json.loads(command([python,'-I','-c',"from knowbase.build_identity import build_id;from knowbase import __version__;import json;print(json.dumps({'version':__version__,'build_id':build_id()}))"]).decode())
                info.update(source_commit=target,configured_runtime=str(python),project_sha256=__import__('hashlib').sha256((source/'pyproject.toml').read_bytes()).hexdigest(),version_policy='single in-place runtime')
                module.atomic(root/'active.json',info);module.atomic(root/'manifest.json',{**info,'backup':str(backup) if backup else None,'state':'bootstrap_verified'})
                command([python,'-I','-m','knowbase','worker','install'],env={'KNOWBASE_MANAGED_UPDATE':'1'},timeout=60)
            except Exception:
                if profiles_started:
                    journals=[str(p.resolve()) for p in (home/'.knowbase/backups').glob('init-*/journal.json') if str(p.resolve()) not in previous_journals]
                    # Undo only our new CAS-journaled profile/settings writes; preserve concurrent edits.
                    repair="import sys;from pathlib import Path;from knowbase import integration;from knowbase.updates import control;control.install_schedule(remove=True);[integration.recover(Path(p)) for p in reversed(sys.argv[1:])];control.install_schedule() if control.read(control.settings_path()).get('enabled') else None"
                    try:command([python,'-I','-c',repair,*journals],timeout=60)
                    except Exception:pass  # Original error and durable journals remain available.
                if old_wheel and old_wheel.exists():command([uv,'pip','install','--python',python,'--offline','--no-deps','--reinstall',old_wheel],timeout=180)
                # Host/profile writes are separately journaled by integration.apply.
                if backup:
                    shutil.copyfile(backup/'config.json',cp);os.chmod(cp,0o600)
                    for name in ['active.json','manifest.json']:
                        if (backup/name).exists():module.atomic(root/name,module.read(backup/name))
                raise
            finally:
                engine.drain.unlink(missing_ok=True)
                if running:engine.worker(True)
        if args.member_id:
            command([python,'-I','-m','knowbase','team','enroll','--member-id',args.member_id,'--label',args.label or args.member_id]+(['--publisher'] if args.publisher else []),timeout=30)
    print('Initial enrollment complete. Start a new/reconnected Agent session once; subsequent program updates are scheduled automatically.')


if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);raise SystemExit(2)
