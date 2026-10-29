"""Cross-platform launch contracts; visible Windows desktop effect needs a real PC."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from knowbase import integration, processes
from knowbase.team import worker
from knowbase.updates import engine


def test_windowless_launcher_preserves_arguments_output_and_failure(tmp_path,monkeypatch):
    log=tmp_path/'logs/task.log';seen=[]
    argv=['-I','-c','print("hello")','path with spaces']
    def run(args,**kw):
        seen.append((args,kw));kw['stderr'].write(b'fixture diagnostic\n')
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(worker.subprocess,'run',run)
    with pytest.raises(SystemExit) as error:
        exec(worker.windows_launcher('C:/fixed/python.exe',argv,log),{})
    assert error.value.code==7
    assert seen[0][0]==['C:/fixed/python.exe',*argv]
    assert seen[0][1]['creationflags']==0x08000000
    assert seen[0][1]['stdin']==worker.subprocess.DEVNULL
    assert log.read_bytes()==b'fixture diagnostic\n'


def test_standalone_updater_hides_git_and_keeps_process_group(monkeypatch):
    seen=[]
    monkeypatch.setattr(engine,'os',SimpleNamespace(name='nt',environ=os.environ))
    monkeypatch.setattr(engine.subprocess,'CREATE_NEW_PROCESS_GROUP',0x200,raising=False)
    monkeypatch.setattr(engine.subprocess,'Popen',lambda args,**kw:seen.append(kw) or SimpleNamespace(
        pid=0,returncode=0,communicate=lambda **_: (b'result',b''),poll=lambda:0))
    # 正常结束后 Windows 也会执行 taskkill 收尾（本机无 taskkill，桩掉）
    monkeypatch.setattr(engine.subprocess,'run',lambda args,**kw:SimpleNamespace(returncode=0))
    assert engine.command(['git','fetch'])==b'result'
    assert seen[0]['creationflags']==0x08000200
    assert seen[0]['stdout']==engine.subprocess.PIPE


def test_package_window_option_is_windows_only(monkeypatch):
    monkeypatch.setattr(processes,'os',SimpleNamespace(name='nt'))
    assert processes.no_window()=={'creationflags':0x08000000}
    monkeypatch.setattr(processes,'os',SimpleNamespace(name='posix'))
    assert processes.no_window()=={}


def test_new_windowless_registered_task_is_owned(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    xml=worker.windows_xml(python,tmp_path/'config.json',900)
    def run(args,**kw):
        return SimpleNamespace(returncode=0,stdout=b'"\\knowbase-team-stats","N/A","Ready"\n' if '/FO' in args else xml)
    monkeypatch.setattr(worker.subprocess,'run',run)
    saved=worker._windows_snapshot(python)
    assert ET.fromstring(saved).find('.//{'+worker.NS+'}Command').text==str(python.with_name('pythonw.exe'))


TASK_DESCRIPTION="本地统计采集器"
TASK_XML=(
    '<?xml version="1.0" encoding="UTF-16" standalone="yes"?>'
    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.3">'
    '<RegistrationInfo><Description>'+TASK_DESCRIPTION+'</Description></RegistrationInfo>'
    '<Actions Context="Collector"><Exec><Command>{command}</Command></Exec></Actions>'
    '</Task>'
)


def _snapshot(python,xml_bytes,monkeypatch,task_name=worker.WINDOWS_TASK):
    def run(args,**kw):
        return SimpleNamespace(returncode=0,stdout=b'"\\knowbase-team-stats","N/A","Ready"\n' if '/FO' in args else xml_bytes)
    monkeypatch.setattr(worker.subprocess,'run',run)
    return worker._windows_snapshot(python,task_name,TASK_DESCRIPTION)


def test_oem_encoded_task_xml_is_decoded_and_owned(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    raw=TASK_XML.format(command=str(python.with_name('pythonw.exe'))).encode('cp936')
    saved=_snapshot(python,raw,monkeypatch)
    task=ET.fromstring(saved)
    assert task.find('.//{'+worker.NS+'}Description').text==TASK_DESCRIPTION


def test_utf16_bom_task_xml_is_decoded(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    raw=TASK_XML.format(command=str(python.with_name('pythonw.exe'))).encode('utf-16')
    saved=_snapshot(python,raw,monkeypatch)
    assert ET.fromstring(saved).find('.//{'+worker.NS+'}Description').text==TASK_DESCRIPTION


def test_bomless_utf16_task_xml_is_decoded(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    raw=TASK_XML.format(command=str(python.with_name('pythonw.exe'))).encode('utf-16-le')
    saved=_snapshot(python,raw,monkeypatch)
    assert ET.fromstring(saved).find('.//{'+worker.NS+'}Description').text==TASK_DESCRIPTION


def test_corrupt_task_xml_cannot_be_verified(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    raw='<?xml version="1.0" encoding="UTF-16"?><Task><Broken>'.encode('cp936')
    with pytest.raises(RuntimeError,match='cannot be verified'):
        _snapshot(python,raw,monkeypatch)


def test_oem_encoded_foreign_task_is_not_owned(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    raw=TASK_XML.format(command='other.exe').encode('cp936')
    with pytest.raises(RuntimeError,match='not an owned'):
        _snapshot(python,raw,monkeypatch)


def test_task_listing_in_console_code_page_is_detected(tmp_path,monkeypatch):
    python=tmp_path/'python.exe'
    name="本地统计任务"
    listing=('"'+name+'","N/A","Ready"\n').encode('cp936')
    def run(args,**kw):
        return SimpleNamespace(returncode=0,stdout=listing if '/FO' in args else b'')
    monkeypatch.setattr(worker.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='cannot be verified'):
        worker._windows_snapshot(python,name,TASK_DESCRIPTION)


def test_windows_probe_never_requests_terminate_rights(windows_probe):
    """Opening a handle for probing must not carry PROCESS_TERMINATE."""
    calls=windows_probe()
    assert engine._windows_alive(4321) is True
    access=[c[1] for c in calls if c[0]=='OpenProcess'][0]
    assert access==engine._WINDOWS_SYNCHRONIZE|engine._WINDOWS_QUERY_LIMITED_INFORMATION
    assert access&0x0001==0  # PROCESS_TERMINATE must never be requested
    assert not [c for c in calls if 'Terminate' in c[0]]


def test_windows_probe_reports_running_and_exited_processes(windows_probe):
    calls=windows_probe(wait=engine._WINDOWS_WAIT_TIMEOUT,exit_code=engine._WINDOWS_STILL_ACTIVE)
    assert engine._windows_alive(4321) is True
    assert engine._windows_alive(4322) is True
    calls=windows_probe(wait=engine._WINDOWS_WAIT_OBJECT_0,exit_code=0)
    assert engine._windows_alive(4321) is False


def test_windows_probe_keeps_access_denied_process_alive(windows_probe):
    """A foreign process cannot be opened, but it exists; overwriting it would be unsafe."""
    windows_probe(open_error='denied',last_error=engine._WINDOWS_ERROR_ACCESS_DENIED)
    assert engine._windows_alive(4321) is True


def test_windows_probe_reports_missing_process(windows_probe):
    windows_probe(open_error='missing',last_error=87)  # ERROR_INVALID_PARAMETER
    assert engine._windows_alive(4321) is False


def test_windows_probe_falls_back_to_exit_code_and_closes_handle(windows_probe):
    calls=windows_probe(wait=0xFFFFFFFF,exit_code=engine._WINDOWS_STILL_ACTIVE)  # WAIT_FAILED
    assert engine._windows_alive(4321) is True
    assert ('CloseHandle',0x1234) in calls


def test_windows_probe_rejects_invalid_pid(windows_probe):
    calls=windows_probe()
    assert engine.alive('not-a-pid') is False
    assert engine.alive(None) is False
    assert engine.alive(0) is False
    assert engine.alive(-1) is False
    assert calls==[]


def test_windows_probe_never_calls_os_kill(windows_probe,monkeypatch):
    """os.kill on Windows terminates the target; probing must not use it."""
    calls=windows_probe(wait=engine._WINDOWS_WAIT_TIMEOUT,exit_code=engine._WINDOWS_STILL_ACTIVE)
    def kill(*args):raise AssertionError('os.kill would terminate the inspected process')
    monkeypatch.setattr(engine,'os',SimpleNamespace(name='nt',kill=kill,environ=os.environ))
    assert engine.alive(4321) is True
    assert calls


def test_posix_probe_still_uses_signal_zero(monkeypatch):
    seen=[]
    def kill(pid,sig):
        seen.append((pid,sig))
        if pid==999:raise PermissionError
        if pid==998:raise ProcessLookupError
    monkeypatch.setattr(engine,'os',SimpleNamespace(name='posix',kill=kill,environ=os.environ))
    assert engine.alive(4321) is True
    assert seen[-1]==(4321,0)
    assert engine.alive(999) is True
    assert engine.alive(998) is False


def test_trae_windows_roaming_path_not_macos_path(tmp_path,monkeypatch):
    from knowbase import config
    monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'.knowbase/config.json')
    monkeypatch.setattr(integration.sys,'platform','win32')
    monkeypatch.setenv('APPDATA',str(tmp_path/'AppData/Roaming'))
    changes=integration.plan(['trae'],tmp_path,managed=True)
    paths={c['path'] for c in changes}
    target=tmp_path/'AppData/Roaming/Trae CN/User/mcp.json'
    assert str(target) in paths and not any('Library/Application Support' in p for p in paths)
    integration.apply(changes,tmp_path)
    assert integration.plan(['trae'],tmp_path,managed=True)==[]
    own=json.loads(target.read_text())['mcpServers']['knowbase']
    assert own['command'].endswith('Scripts/python.exe') and 'mcp_bridge.py' in own['args'][1]


def test_windows_timezone_is_declared_for_clean_installs():
    try:import tomllib
    except ImportError:import tomli as tomllib
    from packaging.requirements import Requirement
    project=tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text())
    req=next(Requirement(x) for x in project['project']['dependencies'] if x.startswith('tzdata'))
    assert req.marker.evaluate({'sys_platform':'win32'})
    assert not req.marker.evaluate({'sys_platform':'darwin'})
