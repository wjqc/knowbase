"""Two disposable member homes: real Git, wheel builds, installs, MCP transport and rollback.

No production interpreter/site-packages, user data, scheduler or company host is modified.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

from knowbase.updates.engine import atomic,module_hash


def run(args,*,cwd=None,env=None,timeout=180,check=True):
    result=subprocess.run([str(a) for a in args],cwd=cwd,env={**os.environ,**(env or {})},capture_output=True,text=True,timeout=timeout)
    if check:assert result.returncode==0,result.stderr[-2000:]+result.stdout[-1000:]
    return result


def git(repo,*args):
    return run(['git','-C',repo,'-c','user.name=fixture','-c','user.email=fixture@example.invalid',*args]).stdout.strip()


@pytest.mark.skipif(shutil.which('uv') is None,reason='uv required for isolated installation integration')
def test_two_members_automatically_update_and_recover_without_host_reconnect():
    async def scenario(root):
        checkout=Path(__file__).resolve().parents[1];source=root/'program';source.mkdir();(source/'.gitignore').write_text('build/\n*.egg-info/\n__pycache__/\n')
        shutil.copytree(checkout/'knowbase',source/'knowbase',ignore=shutil.ignore_patterns('__pycache__'))
        project=(checkout/'pyproject.toml').read_text().replace('version = "0.2.0rc9"','version = "0.2.0rc8"')
        (source/'pyproject.toml').write_text(project)
        init=source/'knowbase/__init__.py';init.write_text(init.read_text().replace('0.2.0rc9','0.2.0rc8'))
        git(source,'init','-q','-b','main');git(source,'add','-A');git(source,'commit','-qm','baseline')
        baseline=git(source,'rev-parse','HEAD')
        dist=root/'baseline-wheel';run(['uv','build','--wheel','--offline','--out-dir',dist,source])
        wheel=next(dist.glob('*.whl'))
        from test_p0_evidence_repair import CARD
        clients=[]
        for name in ['member-a','member-b']:
            home=root/name;runtime=home/'.knowbase/runtime';run(['uv','venv','--python',sys.executable,runtime/'venv'])
            python=runtime/('venv/Scripts/python.exe' if os.name=='nt' else 'venv/bin/python')
            pure=Path(run([python,'-I','-c',"import sysconfig;print(sysconfig.get_path('purelib'))"]).stdout.strip())
            # Reuse only dependency libraries, never a second copy of the user's runtime.
            (pure/'fixture-dependencies.pth').write_text(sysconfig.get_path('purelib')+'\n')
            run(['uv','pip','install','--python',python,'--offline','--no-deps',wheel])
            cp=home/'.knowbase/config.json';repo=home/'knowledge'
            atomic(cp,{'repo_path':str(repo),'knowledge_path':'','git':{'auto_pull':False,'auto_push':False,'auto_commit':False},
                       'embedding':{'enabled':False},'team':{'enabled':False},'automation':{'enabled':False}})
            env={'HOME':str(home),'KNOWBASE_CONFIG':str(cp),'KNOWBASE_RUNTIME_ROOT':str(runtime),
                 'KNOWBASE_MANAGED_UPDATE':'1','KNOWBASE_REPO_PATH':str(repo),
                 'UV_CACHE_DIR':run(['uv','cache','dir']).stdout.strip()}
            run([python,'-I','-m','knowbase','init','--non-interactive'],env=env)
            code="from knowbase.server import save_impl;print(save_impl('pitfall','Synthetic update fixture',"+repr(CARD)+",scope='fixture'))"
            saved=run([python,'-I','-c',code],env=env).stdout
            import re
            mid=re.search(r'已保存 (P-[a-z0-9-]+)',saved).group(1)
            atomic(runtime/'active.json',{'source_commit':baseline,'version':'0.2.0rc8',
                'build_id':module_hash(pure/'knowbase'),'project_sha256':__import__('hashlib').sha256((source/'pyproject.toml').read_bytes()).hexdigest()})
            for src,target in [('engine.py','updater.py'),('bridge.py','mcp_bridge.py'),('entry.py','entry.py')]:
                shutil.copyfile(checkout/'knowbase/updates'/src,runtime/target)
            settings=home/'.knowbase/updates.json';atomic(settings,{'enabled':True,'home':str(home),'runtime_python':str(python),
                'source_url':source.as_uri(),'allow_file_source':True,'uv_path':shutil.which('uv'),'config_path':str(cp),
                'manage_worker':False,'max_wait_seconds':10})
            clients.append((home,runtime,python,cp,env,mid))
        init.write_text(init.read_text().replace('0.2.0rc8','0.2.0rc9'))
        (source/'pyproject.toml').write_text(project.replace('version = "0.2.0rc8"','version = "0.2.0rc9"'))
        git(source,'add','-A');git(source,'commit','-qm','release rc9');release=git(source,'rev-parse','HEAD')

        def update(client,crash=False):
            home,runtime,python,cp,env,mid=client
            if crash:
                code="import importlib.util,sys;s=importlib.util.spec_from_file_location('standalone_updater',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);old=m.Engine.install\ndef install(self,wheel):\n old(self,wheel)\n raise SystemExit(99)\nm.Engine.install=install;print(m.Engine(sys.argv[2]).run(True))"
                return run([python,'-I','-c',code,runtime/'updater.py',home/'.knowbase/updates.json'],env=env,check=False)
            result=run([python,'-I',runtime/'updater.py','run','--force','--settings',home/'.knowbase/updates.json'],env=env)
            return json.loads(result.stdout)

        async def connected(client,callback,expected='0.2.0rc8'):
            home,runtime,python,cp,env,mid=client
            params=StdioServerParameters(command=str(python),args=['-I',str(runtime/'mcp_bridge.py')],env=env)
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    old=await session.call_tool('memory_read',{'id':mid})
                    assert 'runtime_version='+expected in old.content[0].text
                    await callback(session)

        async def first(session):
            interrupted=await asyncio.to_thread(update,clients[0],True)
            assert interrupted.returncode==99,interrupted.stdout+'\n'+interrupted.stderr
            recovered=await asyncio.to_thread(update,clients[0])
            assert recovered['reason']=='interrupted_update_rolled_back',recovered
            old=await session.call_tool('memory_read',{'id':clients[0][-1]})
            assert 'runtime_version=0.2.0rc8' in old.content[0].text
            result=await asyncio.to_thread(update,clients[0]);assert result['state']=='updated',result
            new=await session.call_tool('memory_read',{'id':clients[0][-1]})
            assert 'runtime_version=0.2.0rc9' in new.content[0].text
            assert json.loads((clients[0][1]/'active.json').read_text())['source_commit']==release
            # A candidate that fails only against the real store must roll back in-place.
            index_file=source/'knowbase/index.py';index_file.write_text(index_file.read_text()+
                '\n_previous_connect=connect\ndef connect(repo):\n if (Path(repo)/".postinstall-fail").exists(): raise RuntimeError("fixture")\n return _previous_connect(repo)\n')
            (clients[0][0]/'knowledge/.postinstall-fail').write_text('fixture')
            git(source,'add','-A');git(source,'commit','-qm','bad release fixture')
            failed=await asyncio.to_thread(update,clients[0])
            assert failed['reason']=='candidate_failed_rolled_back',failed
            good=await session.call_tool('memory_read',{'id':clients[0][-1]})
            assert 'runtime_version=0.2.0rc9' in good.content[0].text
            assert json.loads((clients[0][1]/'active.json').read_text())['source_commit']==release
            index_file.write_text(index_file.read_text().split('\n_previous_connect=connect')[0])
            git(source,'add','-A');git(source,'commit','-qm','fixed release fixture')

        await connected(clients[0],first)
        # Bootstrap an old member once. Substitute only OS scheduler ownership;
        # Git, package snapshot, build, migration and profile enrollment remain real.
        scheduler=source/'knowbase/updates/control.py'
        scheduler.write_text(scheduler.read_text()+"\ndef install_schedule(*,remove=False):return {'fixture_scheduler':True}\n")
        worker=source/'knowbase/automation/worker.py'
        worker.write_text(worker.read_text()+"\ndef install(*args,**kwargs):return {'fixture_worker':True}\n")
        git(source,'add','-A');git(source,'commit','-qm','fixture scheduler substitution')
        alias=root/'gitconfig'
        alias.write_text('[url "'+source.as_uri()+'"]\n    insteadOf = https://fixture.invalid/program.git\n')
        bootstrap=checkout/'scripts/bootstrap_auto_update.py'
        code="import sys,importlib.util;sys.path.insert(0,sys.argv.pop(1));from knowbase.updates import engine;engine.Engine.worker=lambda *a:None;engine.Engine.worker_running=lambda *a:False;s=importlib.util.spec_from_file_location('bootstrap_fixture',sys.argv.pop(1));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.main(engine_module=engine)"
        client=clients[1]
        run([client[2],'-I','-c',code,checkout,bootstrap,'--source','https://fixture.invalid/program.git'],
            env={**client[4],'GIT_CONFIG_GLOBAL':str(alias),'UV_OFFLINE':'1'},timeout=240)
        assert json.loads((client[1]/'active.json').read_text())['version']=='0.2.0rc9'
        # Normal scheduled updates have no scheduler substitutions in the test harness.
        settings=client[0]/'.knowbase/updates.json';cfg=json.loads(settings.read_text())
        cfg.update(manage_worker=False,source_url=source.as_uri(),allow_file_source=True)
        atomic(settings,cfg)
        # Deliberately re-enroll the local fixture source (normally an explicit operator action).
        marker=client[0]/'.knowbase/updates/source.json';marker.unlink()
        init.write_text(init.read_text()+'\n# next unattended source change\n')
        git(source,'add','-A');git(source,'commit','-qm','next automatic update')
        async def second(session):
            result=await asyncio.to_thread(update,clients[1]);assert result['state']=='updated',result
            new=await session.call_tool('memory_read',{'id':clients[1][-1]})
            assert 'runtime_version=0.2.0rc9' in new.content[0].text
        await connected(clients[1],second,expected='0.2.0rc9')
        assert (clients[0][0]/'knowledge/memory.db').exists() and (clients[1][0]/'knowledge/memory.db').exists()

    with tempfile.TemporaryDirectory(prefix='knowbase-member-update-fixture-') as directory:
        asyncio.run(scenario(Path(directory)))
