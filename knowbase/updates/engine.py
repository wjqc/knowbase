"""Standalone updater, copied outside site-packages. No knowbase imports here.

The source cache is Git history, not a second runtime. Wheels and shadow code
are temporary. Only the fixed interpreter is installed/used in production.
"""
import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import shlex
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

def no_window():
    return {'creationflags':0x08000000} if os.name=='nt' else {}



class UpdateError(Exception):pass


def validate_source(url,branch='main',*,allow_file=False,allow_insecure=False):
    parsed=urlsplit(url)
    if (not url or '\n' in url or url.startswith('-') or (parsed.username and parsed.scheme!='ssh') or parsed.password
            or parsed.query or parsed.fragment or parsed.scheme not in {'https','http','ssh','file'}):raise UpdateError('invalid_program_source')
    if parsed.scheme=='file' and not allow_file:raise UpdateError('file_source_not_authorized')
    if parsed.scheme=='http' and not allow_insecure:raise UpdateError('tls_source_required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,120}',branch) or '..' in branch:raise UpdateError('invalid_update_branch')


def atomic(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
    with temp.open('x',encoding='utf-8') as f:
        os.chmod(temp,0o600);json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def read(path, default=None):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else ({} if default is None else default)
    except (OSError,ValueError):return {} if default is None else default


_WINDOWS_SYNCHRONIZE=0x100000
_WINDOWS_QUERY_LIMITED_INFORMATION=0x1000
_WINDOWS_ERROR_ACCESS_DENIED=5
_WINDOWS_STILL_ACTIVE=259
_WINDOWS_WAIT_OBJECT_0=0x00000000
_WINDOWS_WAIT_TIMEOUT=0x00000102


class _WindowsProcessApi:
    """Read-only Win32 process queries; never signals the target process."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes=ctypes
        self._wintypes=wintypes
        self._error=0
        kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel32.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel32.OpenProcess.restype=wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel32.WaitForSingleObject.restype=wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype=wintypes.BOOL
        kernel32.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel32.CloseHandle.restype=wintypes.BOOL
        self._kernel32=kernel32

    def open_process(self,pid):
        handle=self._kernel32.OpenProcess(
            _WINDOWS_SYNCHRONIZE|_WINDOWS_QUERY_LIMITED_INFORMATION,False,pid)
        # The last error must be read before any other Win32 call resets it.
        self._error=0 if handle else self._ctypes.get_last_error()
        return handle

    def last_error(self):
        return self._error

    def wait(self,handle):
        return self._kernel32.WaitForSingleObject(handle,0)

    def exit_code(self,handle):
        code=self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(handle,self._ctypes.byref(code)):return None
        return code.value

    def close(self,handle):
        self._kernel32.CloseHandle(handle)


_WINDOWS_API=None


def _windows_api():
    global _WINDOWS_API
    if _WINDOWS_API is None:_WINDOWS_API=_WindowsProcessApi()
    return _WINDOWS_API


def _windows_alive(pid,api=None):
    """Liveness probe on Windows that cannot terminate the inspected process.

    ``os.kill(pid,0)`` is not a probe on Windows: CPython routes it to
    ``TerminateProcess``, so probing would kill the very client the updater
    is waiting for. Open a query-only handle and ask the kernel instead.
    """
    api=api or _windows_api()
    handle=api.open_process(pid)
    if not handle:
        # ACCESS_DENIED means the process exists but belongs to another user.
        return api.last_error()==_WINDOWS_ERROR_ACCESS_DENIED
    try:
        state=api.wait(handle)
        if state==_WINDOWS_WAIT_TIMEOUT:return True
        if state==_WINDOWS_WAIT_OBJECT_0:return False
        # WAIT_FAILED or any unexpected state: fall back to the exit code.
        code=api.exit_code(handle)
        return code is None or code==_WINDOWS_STILL_ACTIVE
    finally:
        api.close(handle)


def alive(pid):
    """Whether a PID is still running, without disturbing that process.

    POSIX: ``os.kill(pid,0)`` only probes existence. Windows: the same call
    terminates the target, so the probe must go through Win32 handles.
    """
    try:
        pid=int(pid)
    except (TypeError,ValueError):
        return False
    if pid<=0:
        return False
    if os.name=='nt':
        return _windows_alive(pid)
    try:
        os.kill(pid,0);return True
    except PermissionError:
        return True
    except OSError:
        return False


class Lock:
    def __init__(self,path):self.path=path;self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);self.file=self.path.open('a+')
        try:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0);self.file.write('0');self.file.flush();self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:self.file.close();raise UpdateError('update_already_running') from None
        return self
    def __exit__(self,*a):self.file.close()


def command(args, *, cwd=None, env=None, timeout=120):
    child_env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never','SSH_ASKPASS_REQUIRE':'never'}
    if env:child_env.update(env)
    process=None
    try:
        process=subprocess.Popen([str(x) for x in args],cwd=cwd,env=child_env,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            **({'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}))
        out,_=process.communicate(timeout=timeout)
        if process.returncode:raise UpdateError('subprocess_failed')
        return out
    except (OSError,subprocess.SubprocessError):raise UpdateError('subprocess_unavailable_or_timed_out') from None
    finally:
        if process is not None:
            # 正常结束后 Windows 也必须收尾：孙进程（node/MCP 子服务）会继续存活
            # 并锁住 venv 文件；taskkill 对已退出 PID 报错可容忍（输出被丢弃）。
            if os.name=='nt':subprocess.run(['taskkill','/F','/T','/PID',str(process.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5, **no_window())
            else:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:pass


def module_hash(package):
    h=hashlib.sha256()
    for path in sorted(p for p in package.rglob('*') if p.is_file() and
                       (p.suffix=='.py' or p.parent==package/'data' and p.suffix=='.json')):
        h.update(str(path.relative_to(package)).encode());h.update(path.read_bytes())
    return h.hexdigest()[:16]


LOG_TABLES=('usage_log','feedback_log','knowledge_uses','feedback_events','task_outcomes',
            'jobs','job_spool','candidates','job_submissions','value_outbox','value_identity','command_calls',
            'context_sessions','context_events','job_segments','job_batches','segment_results',
            'applied_operations','entity_heads','write_intents','feedback_requests','command_call_hosts',
            'card_conflicts','retrieval_deliveries','use_retrieval_links','task_evidence','capture_gaps',
            'project_bindings','executor_runs','automation_budget')


def ledger_snapshot(db, columns=None):
    if not Path(db).exists():return {}
    c=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")};result={}
        for table in LOG_TABLES:
            if table not in tables:continue
            names=(columns or {}).get(table,{}).get('columns') or [r[1] for r in c.execute(f'PRAGMA table_info({table})')]
            query=','.join('"'+n.replace('"','""')+'"' for n in names)
            rows=sorted(json.dumps(row,ensure_ascii=False,default=str) for row in c.execute(f'SELECT {query} FROM {table}'))
            result[table]={'columns':names,'count':len(rows),'hash':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
        return result
    finally:c.close()


def cards(repo):
    return {str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in repo.rglob('*.md') if '.git' not in p.relative_to(repo).parts and p.name!='INDEX.md'}


def ledger_unchanged(db, before):
    after=ledger_snapshot(db,before)
    return all(after.get(k)==v for k,v in before.items()) and all(v['count']==0 for k,v in after.items() if k not in before)


def snapshot_wheel(directory):
    """Transient bootstrap rollback artifact; never retained as another runtime."""
    import importlib.metadata
    try:dist=importlib.metadata.distribution('knowbase')
    except importlib.metadata.PackageNotFoundError:return None
    package=Path(dist.locate_file('knowbase'))
    if not (package/'__init__.py').exists():raise UpdateError('legacy_editable_install_requires_migration')
    version=dist.version;name=f'knowbase-{version}.dist-info';files={}
    for p in package.rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:files['knowbase/'+str(p.relative_to(package))]=p.read_bytes()
    for item in dist.files or []:
        parts=Path(str(item)).parts
        if len(parts)>=2 and parts[0].endswith('.dist-info') and parts[-1] not in {'RECORD','INSTALLER','REQUESTED','direct_url.json'}:
            p=Path(dist.locate_file(item))
            if p.is_file():files[name+'/'+str(Path(*parts[1:]))]=p.read_bytes()
    files.setdefault(name+'/WHEEL',b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    rows=[(path,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),str(len(data))) for path,data in files.items()]
    rows.append((name+'/RECORD','',''));record=io.StringIO();csv.writer(record,lineterminator='\n').writerows(rows);files[name+'/RECORD']=record.getvalue().encode()
    target=Path(directory)/f'knowbase-{version}-py3-none-any.whl'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for path,data in files.items():z.writestr(path,data)
    target.chmod(0o600);return target


def rpc_probe(python, root, config_path, protocol='2025-03-26', schema=False):
    bootstrap="import sys;sys.path.insert(0,sys.argv.pop(1));from knowbase.server import main;main()"
    env={**os.environ,'KNOWBASE_CONFIG':str(config_path),'KNOWBASE_AGENT_NAME':'update-probe'}
    env.pop('KNOWBASE_REPO_PATH',None)
    proc=subprocess.Popen([str(python),'-I','-B','-c',bootstrap,str(root)],env=env,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True, **no_window())
    messages=queue.Queue()
    def reader():
        for line in proc.stdout:
            try:messages.put(json.loads(line))
            except ValueError:messages.put({'error':'invalid_protocol_output'})
    threading.Thread(target=reader,daemon=True).start()
    def exchange(value):
        proc.stdin.write(json.dumps(value)+'\n');proc.stdin.flush();until=time.monotonic()+15
        while time.monotonic()<until:
            try:message=messages.get(timeout=max(.01,until-time.monotonic()))
            except queue.Empty:break
            if message.get('id')==value['id']:
                if message.get('error'):raise UpdateError('mcp_probe_failed')
                return message['result']
        raise UpdateError('mcp_probe_timed_out')
    try:
        init=exchange({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':protocol,
            'capabilities':{},'clientInfo':{'name':'knowbase-updater','version':'1'}}})
        if init.get('protocolVersion')!=protocol:raise UpdateError('mcp_protocol_incompatible')
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');proc.stdin.flush()
        tools=exchange({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools'];names={x['name'] for x in tools}
        if not {'memory_search','memory_read','memory_save','memory_context','memory_use','memory_feedback'} <= names:
            raise UpdateError('mcp_tools_missing')
        return tools if schema else len(names)
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()


class Engine:
    def __init__(self, settings):
        self.settings_path=Path(settings).resolve();self.cfg=read(self.settings_path)
        self.home=Path(self.cfg.get('home',str(Path.home()))).resolve();self.root=self.home/'.knowbase/runtime'
        expected=self.root/('venv/Scripts/python.exe' if os.name=='nt' else 'venv/bin/python')
        runtime=Path(self.cfg.get('runtime_python',str(expected)))
        self.python=runtime.parent.resolve()/runtime.name
        if self.python!=expected:raise UpdateError('fixed_runtime_required')
        self.directory=self.home/'.knowbase/updates';self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.state_path=self.directory/'state.json';self.journal=self.directory/'journal.json'
        self.cache=self.directory/'source.git';self.drain=self.root/'update-drain.json'
        self.config_path=Path(self.cfg.get('config_path',str(self.home/'.knowbase/config.json'))).resolve()
        self.app=read(self.config_path);self.repo=Path(os.path.expanduser(self.app.get('repo_path',str(self.home/'knowbase')))).resolve()
        self.uv=self.cfg.get('uv_path') or shutil.which('uv')
        if not self.uv:raise UpdateError('uv_missing')
        self.branch=self.cfg.get('branch','main');self.url=self.cfg.get('source_url','')
        validate_source(self.url,self.branch,allow_file=self.cfg.get('allow_file_source'),allow_insecure=self.cfg.get('allow_insecure_source'))

    def status(self):return read(self.state_path)

    def check(self):
        with Lock(self.directory/'.lock'),tempfile.TemporaryDirectory(prefix='knowbase-update-check-') as td:
            target=self.fetch();source=self.export(target,Path(td)/'source');info=self.source_info(source)
            active=read(self.root/'active.json')
            return {'state':'up_to_date' if active.get('build_id')==info['build_id'] and active.get('project_sha256')==info['project_sha256'] else 'available_not_applied',
                    'source_commit':target,**info}

    def git(self,*args):
        return command(['git','-c','core.hooksPath='+os.devnull,'-c','http.sslVerify=true',
                        '--git-dir',self.cache,*args],timeout=60).decode().strip()

    def fetch(self):
        marker=self.directory/'source.json'
        if marker.exists() and read(marker).get('source_url')!=self.url:raise UpdateError('source_change_requires_reenrollment')
        if not self.cache.exists():command(['git','init','--bare',self.cache])
        if not marker.exists():atomic(marker,{'source_url':self.url})
        self.git('fetch','--no-tags',self.url,'+refs/heads/'+self.branch+':refs/remotes/update/channel')
        target=self.git('rev-parse','refs/remotes/update/channel')
        previous=read(self.root/'active.json').get('source_commit')
        if previous and previous!=target:
            try:self.git('merge-base','--is-ancestor',previous,target)
            except UpdateError:raise UpdateError('non_fast_forward_release') from None
        return target

    def export(self, commit, directory):
        archive=command(['git','--git-dir',self.cache,'archive',commit],timeout=60)
        directory.mkdir(parents=True,exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            total=0
            for item in tar.getmembers():
                path=Path(item.name);total+=item.size
                if path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()) or total>128*1024*1024:
                    raise UpdateError('unsafe_source_archive')
            tar.extractall(directory)
        return directory

    def source_info(self, source):
        try:
            import tomllib
        except ImportError:import tomli as tomllib
        metadata=tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'));project=metadata['project']
        version=None
        if metadata.get('tool',{}).get('knowbase',{}).get('updates',{}).get('protocol')!=1:
            raise UpdateError('unsupported_update_protocol')
        if not all((source/'knowbase/updates'/name).is_file() for name in ['engine.py','bridge.py','entry.py']):
            raise UpdateError('update_entry_missing')
        for node in ast.parse((source/'knowbase/__init__.py').read_text(encoding='utf-8')).body:
            if isinstance(node,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='__version__' for x in node.targets):version=ast.literal_eval(node.value)
        if project['name']!='knowbase' or version!=project['version']:raise UpdateError('package_version_mismatch')
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        if not SpecifierSet(project.get('requires-python','>=3.10')).contains('.'.join(map(str,sys.version_info[:3]))):
            raise UpdateError('python_version_incompatible')
        current=read(self.root/'active.json').get('version')
        if current and Version(version)<Version(current):raise UpdateError('version_downgrade_not_authorized')
        return {'version':version,'build_id':module_hash(source/'knowbase'),
                'project_sha256':hashlib.sha256((source/'pyproject.toml').read_bytes()).hexdigest()}

    def build(self, source, output):
        output.mkdir()
        command([self.uv,'build','--wheel','--offline','--python',self.python,'--out-dir',output,source],timeout=180)
        wheels=list(output.glob('knowbase-*.whl'))
        if len(wheels)!=1:raise UpdateError('wheel_missing')
        return wheels[0]

    def unpack(self, wheel, destination):
        destination.mkdir()
        with zipfile.ZipFile(wheel) as zipped:
            if sum(i.file_size for i in zipped.infolist())>128*1024*1024:raise UpdateError('wheel_too_large')
            for i in zipped.infolist():
                if Path(i.filename).is_absolute() or '..' in Path(i.filename).parts or (i.external_attr>>16)&0o170000==0o120000:
                    raise UpdateError('unsafe_wheel')
            zipped.extractall(destination)
            record=next(n for n in zipped.namelist() if n.endswith('.dist-info/RECORD'))
            for name,hashed,size in csv.reader(io.StringIO(zipped.read(record).decode())):
                if not hashed:continue
                algorithm,expected=hashed.split('=',1)
                if algorithm!='sha256' or base64.urlsafe_b64encode(hashlib.sha256(zipped.read(name)).digest()).rstrip(b'=').decode()!=expected:
                    raise UpdateError('wheel_hash_mismatch')
        return destination

    def dependencies(self, unpacked):
        import email,importlib.metadata
        from packaging.requirements import Requirement
        metadata=email.message_from_bytes(next(unpacked.glob('*.dist-info/METADATA')).read_bytes())
        def normalized(values):return {str(Requirement(v)) for v in values if 'extra ==' not in v}
        if normalized(metadata.get_all('Requires-Dist') or [])!=normalized(importlib.metadata.requires('knowbase') or []):
            raise UpdateError('dependency_change_requires_bootstrap')

    def shadow(self, candidate, directory):
        shadow=directory/'memory';shadow.mkdir()
        command(['git','init','--quiet',shadow])
        for path in self.repo.rglob('*.md'):
            if '.git' in path.relative_to(self.repo).parts:continue
            target=shadow/path.relative_to(self.repo);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        db=self.repo/'memory.db'
        if db.exists():
            source=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);dest=sqlite3.connect(shadow/'memory.db');source.backup(dest);dest.close();source.close()
        before=ledger_snapshot(shadow/'memory.db');before_cards=cards(shadow)
        cp=directory/'config.json';atomic(cp,{'repo_path':str(shadow),'knowledge_path':'',
            'git':{'auto_pull':False,'auto_push':False,'auto_commit':False},'embedding':{'enabled':False},
            'automation':{'enabled':False},'team':{'enabled':False},'value':{'events_enabled':False}})
        self.probe(candidate,cp)
        if not ledger_unchanged(shadow/'memory.db',before) or cards(shadow)!=before_cards:
            raise UpdateError('shadow_user_data_changed')
        new=rpc_probe(self.python,candidate,cp,schema=True)
        old=rpc_probe(self.python,Path(self.git_package_path()),cp,schema=True)
        before={x['name']:x['inputSchema'] for x in old};after={x['name']:x['inputSchema'] for x in new}
        def shape(value):
            if isinstance(value,dict):return {k:shape(v) for k,v in value.items() if k not in {'title','description','default'}}
            if isinstance(value,list):return [shape(v) for v in value]
            return value
        for name,previous in before.items():
            current=after.get(name)
            if current is None or not set(current.get('required',[]))<=set(previous.get('required',[])):
                raise UpdateError('mcp_tool_contract_changed')
            if any(k not in current.get('properties',{}) or shape(v)!=shape(current['properties'][k])
                   for k,v in previous.get('properties',{}).items()):raise UpdateError('mcp_tool_contract_changed')

    def probe(self, candidate, cp):
        code="import sys,json;sys.path.insert(0,sys.argv.pop(1));from knowbase import config,index,doctor;c=index.connect(config.repo_path());c.close();checks=doctor.inspect(config.repo_path());assert not [x for x in checks if x['level']=='fail'];print('ok')"
        env={'KNOWBASE_CONFIG':str(cp),'KNOWBASE_REPO_PATH':str(read(cp).get('repo_path',self.repo)),
             'KNOWBASE_AGENT_NAME':'update-health-check'}
        command([self.python,'-I','-B','-c',code,candidate],env=env,timeout=60)

    def live_bridges(self):
        return [d for p in (self.root/'bridges').glob('*.json') if (d:=read(p)) and alive(d.get('pid'))]

    def processes(self):
        if os.name=='nt':
            # PS 5.1 管道输出走控制台 OEM 代码页（中文系统 cp936），CommandLine
            # 含中文路径时 json 的编码自动检测会失败；先强制会话输出为 UTF-8。
            script=("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                    "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
            raw=command(['powershell','-NoProfile','-NonInteractive','-Command',script],timeout=15)
            try:
                rows=json.loads(raw.decode('utf-8',errors='replace') or '[]')
                if isinstance(rows,dict):rows=[rows]
                return [(int(r['ProcessId']),r.get('CommandLine') or '') for r in rows]
            except (ValueError,KeyError,TypeError):raise UpdateError('subprocess_failed') from None
        raw=command(['ps','-axo','pid=,command='],timeout=15).decode()
        return [(int(p[0]),p[1]) for line in raw.splitlines() if len(p:=line.strip().split(None,1))==2]

    def runtime_args(self, cmd):
        try:
            args=shlex.split(cmd,posix=os.name!='nt')
            executable=Path(args[0].strip('"'))
            return args[1:] if executable.parent.resolve()/executable.name==self.python else None
        except (ValueError,IndexError,OSError):return None

    def quiesce(self):
        atomic(self.drain,{'owner':os.getpid(),'started_at':time.time(),'phase':'drain_requests'})
        end=time.monotonic()+min(120,max(1,int(self.cfg.get('max_wait_seconds',60))))
        while time.monotonic()<end:
            bridges=self.live_bridges();managed={pid for d in bridges for pid in [d.get('backend_pid'),d.get('pid')]}
            if any(Path(d.get('config_path','')).resolve()!=self.config_path for d in bridges):
                raise UpdateError('additional_config_requires_explicit_enrollment')
            legacy=[]
            for pid,cmd in self.processes():
                args=self.runtime_args(cmd)
                if args is None or pid==os.getpid() or pid in managed:continue
                while args and args[0] in {'-I','-B','-u'}:args=args[1:]
                if args[:2]==['-m','knowbase'] and args[2:3] in (['worker'],['update']):continue
                if args and Path(args[0].strip('"')).name in {'mcp_bridge.py','entry.py'} and Path(args[0].strip('"')).parent.resolve()==self.root:continue
                legacy.append(pid)
            entries=[p for p in (self.root/'entries').glob('*.json') if alive(read(p).get('pid'))]
            db=self.repo/'memory.db';busy=False
            if db.exists():
                c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    tables={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
                    if 'automation_lease' in tables:
                        row=c.execute("SELECT owner,lease_until FROM automation_lease WHERE singleton='worker'").fetchone()
                        busy=bool(row and row[0] and row[1]>datetime.now(timezone.utc).isoformat(timespec='seconds'))
                    if 'jobs' in tables:busy=busy or bool(c.execute("SELECT 1 FROM jobs WHERE state='claimed' AND lease_until>? LIMIT 1",(datetime.now(timezone.utc).isoformat(timespec='seconds'),)).fetchone())
                finally:c.close()
            if not legacy and not entries and not busy and all(d.get('active_requests',0)==0 for d in bridges):
                atomic(self.drain,{'owner':os.getpid(),'started_at':time.time(),'phase':'pause_backends'})
                if all(d.get('phase')=='paused' for d in bridges):return
            time.sleep(.2)
        raise UpdateError('busy_or_legacy_client_reconnect_required')

    def worker(self,start):
        if not self.cfg.get('manage_worker',True):return
        label='com.knowbase.worker'
        if sys.platform=='darwin':
            domain=f'gui/{os.getuid()}';plist=self.home/'Library/LaunchAgents'/f'{label}.plist'
            if start and plist.exists():command(['launchctl','bootstrap',domain,plist],timeout=15)
            elif not start:subprocess.run(['launchctl','bootout',domain+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15, **no_window())
        elif os.name=='nt':
            subprocess.run(['schtasks','/Run' if start else '/End','/TN','knowbase-worker'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15, **no_window())
        else:
            subprocess.run(['systemctl','--user','start' if start else 'stop','knowbase-worker.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15, **no_window())

    def worker_running(self):
        if not self.cfg.get('manage_worker',True):return False
        if sys.platform=='darwin':args=['launchctl','print',f'gui/{os.getuid()}/com.knowbase.worker']
        elif os.name=='nt':
            # schtasks /Query 只要任务注册过就返回 0，不代表正在运行；
            # Get-ScheduledTask 的 State 是英文枚举，不随系统语言本地化。
            try:
                out=command(['powershell','-NoProfile','-NonInteractive','-Command',
                             "(Get-ScheduledTask -TaskName 'knowbase-worker').State.ToString()"],timeout=15)
                return out.decode('utf-8',errors='replace').strip()=='Running'
            except UpdateError:return False  # 任务未注册或调度器不可用：按未运行处理
        else:args=['systemctl','--user','is-active','knowbase-worker.service']
        try:return subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10, **no_window()).returncode==0
        except (OSError,subprocess.SubprocessError):raise UpdateError('scheduler_state_unavailable') from None

    def backup(self):
        directory=self.home/'.knowbase/backups'/('auto-update-'+uuid.uuid4().hex);directory.mkdir(parents=True,mode=0o700)
        for name,path in [('memory.db',self.repo/'memory.db'),('team-state.db',Path(os.path.expanduser(self.app.get('team',{}).get('data_dir',str(self.home/'.knowbase/team'))))/'state.db')]:
            if path.exists():
                source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);dest=sqlite3.connect(directory/name);source.backup(dest);dest.close();source.close();(directory/name).chmod(0o600)
        shutil.copyfile(self.config_path,directory/'config.json');os.chmod(directory/'config.json',0o600)
        for name in ['active.json','manifest.json']:
            if (self.root/name).exists():shutil.copyfile(self.root/name,directory/name);os.chmod(directory/name,0o600)
        with tarfile.open(directory/'knowledge.tar.gz','w:gz') as tar:
            for path in self.repo.rglob('*'):
                if '.git' not in path.relative_to(self.repo).parts and path.is_file() and path.name not in {'memory.db','memory.db-wal','memory.db-shm','.lock'}:tar.add(path,arcname=str(path.relative_to(self.repo)),recursive=False)
        os.chmod(directory/'knowledge.tar.gz',0o600)
        return directory

    def install(self,wheel):
        # Windows 上杀毒/索引进程或存活的 bridge/Hook 进程可能短暂锁住 venv 目标文件；
        # 失败先退避重试再判定，最终失败仍由 run()/bootstrap 的备份回滚兜底。
        attempts=3 if os.name=='nt' else 1
        for i in range(attempts):
            try:
                command([self.uv,'pip','install','--python',self.python,'--offline','--no-cache','--no-deps','--reinstall',wheel],timeout=180)
                return
            except UpdateError:
                if i==attempts-1:raise
                time.sleep(2*(i+1))

    def rollback(self, journal, temporary):
        backup=Path(journal['backup']).resolve();allowed=self.home/'.knowbase/backups'
        if backup.parent!=allowed.resolve() or not backup.name.startswith('auto-update-'):raise UpdateError('invalid_recovery_backup')
        if not ledger_unchanged(self.repo/'memory.db',journal['ledger']) or cards(self.repo)!=journal['cards']:
            raise UpdateError('rollback_would_overwrite_new_data')
        source=self.export(journal['previous_commit'],temporary/'rollback-source')
        wheel=self.build(source,temporary/'rollback-wheel');self.install(wheel)
        source_db=sqlite3.connect(backup/'memory.db');dest=sqlite3.connect(self.repo/'memory.db');source_db.backup(dest);dest.close();source_db.close()
        for name in ['active.json','manifest.json']:
            if (backup/name).exists():atomic(self.root/name,read(backup/name))
        self.refresh_entries(source)
        self.probe(Path(self.git_package_path()),self.config_path)
        journal['phase']='rolled_back';atomic(self.journal,journal)

    def git_package_path(self):
        return command([self.python,'-I','-c',"import sysconfig;print(sysconfig.get_path('purelib'))"]).decode().strip()

    def refresh_entries(self, candidate):
        hashes={}
        for target,name in {'updater.py':'engine.py','mcp_bridge.py':'bridge.py','entry.py':'entry.py'}.items():
            source=Path(candidate)/'knowbase/updates'/name
            if not source.exists():raise UpdateError('update_entry_missing')
            data=source.read_bytes();compile(data,str(source),'exec')
            path=self.root/target;temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
            temp.write_bytes(data);temp.chmod(0o600);os.replace(temp,path);hashes[target]=hashlib.sha256(data).hexdigest()
        atomic(self.root/'managed-update.json',{'schema':'knowbase.update-entries.v1','files':hashes})

    def run(self,force=False):
        with Lock(self.directory/'.lock'):
            state=self.status();journal=read(self.journal)
            if not self.cfg.get('enabled'):return {'state':'disabled'}
            recovering=journal.get('phase') in {'installing','migrating'}
            if not recovering:self.drain.unlink(missing_ok=True)
            if not recovering and not force and state.get('retry_at',0)>time.time():return state
            stopped=False;restart=False;stage='preflight'
            try:
                with tempfile.TemporaryDirectory(prefix='knowbase-update-') as td:
                    temp=Path(td)
                    if not (self.repo/'memory.db').exists():raise UpdateError('database_initialization_required')
                    size=(self.repo/'memory.db').stat().st_size if (self.repo/'memory.db').exists() else 0
                    if shutil.disk_usage(self.home).free<max(100*1024*1024,size*3):raise UpdateError('insufficient_disk_space')
                    if journal.get('phase') in {'installing','migrating'}:
                        stage='interrupted_recovery';self.quiesce();restart=bool(journal.get('worker_running'));self.worker(False);stopped=True;self.rollback(journal,temp)
                        self.drain.unlink(missing_ok=True)
                        if restart:self.worker(True)
                        stopped=False
                        raise UpdateError('interrupted_update_rolled_back')
                    stage='source_validation';target=self.fetch();source=self.export(target,temp/'source');info=self.source_info(source)
                    active=read(self.root/'active.json')
                    installed=Path(self.git_package_path())/'knowbase'
                    if not active.get('source_commit'):
                        # A fresh init may enroll an already installed, exact published build.
                        # Never guess which older source produced a different installation.
                        import importlib.metadata
                        if module_hash(installed)!=info['build_id'] or importlib.metadata.version('knowbase')!=info['version']:
                            raise UpdateError('bootstrap_provenance_required')
                        active={'source_commit':target,**info,'configured_runtime':str(self.python),
                                'version_policy':'single in-place runtime'}
                        atomic(self.root/'active.json',active)
                        atomic(self.root/'manifest.json',{**active,'state':'published_build_matched','verified_at':time.time()})
                    if module_hash(installed)!=active.get('build_id'):raise UpdateError('installed_build_requires_bootstrap_reconciliation')
                    if info['build_id']==active.get('build_id') and info['project_sha256']==active.get('project_sha256'):
                        self.refresh_entries(Path(self.git_package_path()))
                        state={'state':'up_to_date','checked_commit':target,'checked_at':time.time()};atomic(self.state_path,state);return state
                    stage='build';wheel=self.build(source,temp/'wheel');candidate=self.unpack(wheel,temp/'candidate');self.dependencies(candidate)
                    if module_hash(candidate/'knowbase')!=info['build_id']:raise UpdateError('source_wheel_mismatch')
                    stage='shadow_validation';(temp/'shadow').mkdir();self.shadow(candidate,temp/'shadow')
                    stage='drain_and_backup';self.quiesce();restart=self.worker_running();self.worker(False);stopped=True;backup=self.backup()
                    previous=active.get('source_commit')
                    journal={'phase':'installing','target':target,'previous_commit':previous,'backup':str(backup),
                             'ledger':ledger_snapshot(self.repo/'memory.db'),'cards':cards(self.repo),'worker_running':restart}
                    atomic(self.journal,journal)
                    try:
                        stage='installation';self.install(wheel);journal['phase']='migrating';atomic(self.journal,journal)
                        stage='post_install_validation';self.probe(Path(self.git_package_path()),self.config_path)
                        if not ledger_unchanged(self.repo/'memory.db',journal['ledger']) or cards(self.repo)!=journal['cards']:
                            raise UpdateError('real_user_data_changed')
                        atomic(self.root/'active.json',{'configured_runtime':str(self.python),'source_commit':target,
                            **info,'manifest':str(self.root/'manifest.json'),'version_policy':'single in-place runtime'})
                        atomic(self.root/'manifest.json',{'source_commit':target,**info,'backup':str(backup),'runtime':str(self.python),
                            'state':'automatic_upgrade_verified','verified_at':time.time(),'native_host_instructions':'new-session-if-changed'})
                        self.refresh_entries(candidate)
                        journal['phase']='committed';atomic(self.journal,journal)
                    except Exception:
                        self.rollback(journal,temp);raise UpdateError('candidate_failed_rolled_back') from None
                    finally:
                        if read(self.journal).get('phase') in {'committed','rolled_back'}:
                            self.drain.unlink(missing_ok=True)
                            if restart:self.worker(True)
                            stopped=False
                    state={'state':'updated','source_commit':target,**info,'checked_at':time.time(),'backup':str(backup)}
                    atomic(self.state_path,state);return state
            except Exception as exc:
                if read(self.journal).get('phase') not in {'installing','migrating'}:
                    self.drain.unlink(missing_ok=True)
                    if stopped and restart:self.worker(True)
                failures=int(state.get('failures',0))+1
                state={'state':'deferred_or_failed','stage':stage,'reason':str(exc) if isinstance(exc,UpdateError) else 'unexpected_update_failure','failures':failures,'checked_at':time.time(),
                       'retry_at':time.time()+min(21600,max(300,int(self.cfg.get('interval_seconds',900)))*2**min(failures,4))}
                atomic(self.state_path,state);return state


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['run','check','status']);parser.add_argument('--settings',required=True);parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    try:
        engine=Engine(args.settings);result=engine.status() if args.action=='status' else engine.check() if args.action=='check' else engine.run(args.force)
    except UpdateError as exc:result={'state':'failed','reason':str(exc)}
    except Exception:result={'state':'failed','reason':'updater_configuration_or_state_invalid'}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
