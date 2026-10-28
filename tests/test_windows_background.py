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
        returncode=0,communicate=lambda **_: (b'result',b''),poll=lambda:0))
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
