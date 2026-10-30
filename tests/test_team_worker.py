"""Scheduler contract/rollback tests; Windows native execution is separate acceptance."""
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from knowbase import config
from knowbase.team import worker


def prepare(memory_repo,tmp_path,monkeypatch,platform):
    from knowbase.team import service  # Load timezone/system config before platform simulation.
    home=tmp_path/'home space';python=worker.fixed_python(home,platform);python.parent.mkdir(parents=True);python.touch();python.with_name('pythonw.exe').touch()
    monkeypatch.setattr(worker.Path,'home',lambda:home)
    monkeypatch.setattr(worker.sys,'platform',platform)
    cfg=config.load_config();cfg['team']['data_dir']=str(tmp_path/'stats');config.CONFIG_PATH.write_text(json.dumps(cfg))
    return home,python,tmp_path/'stats'


def test_windows_task_has_fixed_runtime_config_no_shell_and_single_instance(memory_repo,tmp_path,monkeypatch):
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'win32');calls=[]
    monkeypatch.setattr(worker.subprocess,'run',lambda args,**kw:(calls.append(args) or SimpleNamespace(returncode=0)))
    result=worker.install();xml=ET.fromstring(Path(result['path']).read_bytes());ns={'t':worker.NS}
    assert xml.find('.//t:Exec/t:Command',ns).text==str(python.with_name('pythonw.exe'))
    args=xml.find('.//t:Exec/t:Arguments',ns).text
    assert 'KNOWBASE_CONFIG' in args and 'runpy.run_module' in args and 'team' in args and 'sync' in args and 'creationflags=0x08000000' in args
    assert xml.find('.//t:LogonType',ns).text=='InteractiveToken'
    assert xml.find('.//t:RunLevel',ns).text=='LeastPrivilege'
    assert xml.find('.//t:MultipleInstancesPolicy',ns).text=='IgnoreNew'
    assert xml.find('.//t:Interval',ns).text=='PT900S'
    assert [a[1] for a in calls]==['/Query','/Create','/Query','/Run']
    worker.install(remove=True);assert not Path(result['path']).exists()


def test_windows_scheduler_reject_restores_descriptor(memory_repo,tmp_path,monkeypatch):
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'win32');root.mkdir();path=root/'worker-task.xml';path.write_text('previous')
    calls=[]
    def run(args,**kw):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[1]=='/Create' and len(calls)==2 else 0)
    monkeypatch.setattr(worker.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='previous configuration restored'):worker.install()
    assert path.read_text()=='previous' and len(calls)==3


def test_launchagent_failure_restores_old_file(memory_repo,tmp_path,monkeypatch):
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'darwin');path=home/'Library/LaunchAgents'/f'{worker.LABEL}.plist';path.parent.mkdir(parents=True)
    old=plistlib.dumps({'Label':worker.LABEL,'ProgramArguments':['old']});path.write_bytes(old);calls=[]
    def run(args,**kw):
        # 两域（gui/user）bootstrap 从第 3 次调用起全部失败：验证回滚恢复旧配置
        calls.append(args);return SimpleNamespace(returncode=1 if args[1]=='bootstrap' and len(calls)>=3 else 0)
    monkeypatch.setattr(worker.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='previous configuration restored'):worker.install()
    assert path.read_bytes()==old and len(calls)==8


def test_linux_scheduler_configuration_and_removal(memory_repo,tmp_path,monkeypatch):
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'linux');calls=[]
    monkeypatch.setattr(worker.subprocess,'run',lambda args,**kw:(calls.append(args) or SimpleNamespace(returncode=0)))
    worker.install()
    assert 'TimeoutStartSec=300' in (home/'.config/systemd/user/knowbase-team-stats.service').read_text()
    assert 'Persistent=true' in (home/'.config/systemd/user/knowbase-team-stats.timer').read_text()
    worker.install(remove=True)
    assert not (home/'.config/systemd/user/knowbase-team-stats.timer').exists()


def test_windows_preserves_unknown_existing_task(memory_repo,tmp_path,monkeypatch):
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'win32');calls=[]
    def run(args,**kw):
        calls.append(args)
        if '/FO' in args:return SimpleNamespace(returncode=0,stdout=b'"\\knowbase-team-stats","N/A","Ready"\n')
        return SimpleNamespace(returncode=0,stdout=b'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>other.exe</Command></Exec></Actions></Task>')
    monkeypatch.setattr(worker.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='not an owned'):worker.install()
    assert not any('/Create' in a or '/Delete' in a for a in calls)


def test_windows_reinstall_accepts_oem_encoded_own_task(memory_repo,tmp_path,monkeypatch):
    """schtasks announces UTF-16 but emits OEM bytes; the owned task must stay reinstallable."""
    home,python,root=prepare(memory_repo,tmp_path,monkeypatch,'win32');calls=[]
    existing=(
        '<?xml version="1.0" encoding="UTF-16" standalone="yes"?>'
        '<Task xmlns="'+worker.NS+'" version="1.3">'
        '<RegistrationInfo><Description>knowbase local team statistics collector</Description></RegistrationInfo>'
        '<Actions Context="Collector"><Exec><Command>'+str(python.with_name('pythonw.exe'))+'</Command></Exec></Actions>'
        '</Task>'
    ).encode('cp936')
    def run(args,**kw):
        calls.append(args)
        if '/FO' in args:return SimpleNamespace(returncode=0,stdout=b'"\\knowbase-team-stats","N/A","Ready"\n')
        if '/XML' in args:return SimpleNamespace(returncode=0,stdout=existing)
        return SimpleNamespace(returncode=0,stdout=b'')
    monkeypatch.setattr(worker.subprocess,'run',run)
    result=worker.install()
    assert [a[1] for a in calls]==['/Query','/Query','/Create','/Query','/Run']
    assert Path(result['path']).exists()
