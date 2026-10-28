"""One OS-owned collector, using the unique fixed runtime on each platform."""
import getpass
import csv
import io
import json
import os
import plistlib
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import config
from ..integration import _atomic

LABEL = "com.knowbase.team-stats"
WINDOWS_TASK = "knowbase-team-stats"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def fixed_python(home=None, platform=None):
    return Path(home or Path.home()) / ".knowbase/runtime/venv" / ("Scripts/python.exe" if (platform or sys.platform) == "win32" else "bin/python")


def _execute(args, *, required=True):
    try:
        result = subprocess.run(args, capture_output=True, timeout=15, **({'creationflags':0x08000000} if os.name=='nt' else {}))
    except (OSError, subprocess.SubprocessError):
        if required:
            raise RuntimeError("background scheduler command failed") from None
        return None
    if required and result.returncode:
        raise RuntimeError("background scheduler rejected configuration")
    return result


def windows_launcher(python, arguments, log_path):
    """Windowless sibling launches the fixed console interpreter with explicit pipes."""
    return ("import os,subprocess;from pathlib import Path;"
            + "p=Path("+repr(str(log_path))+");p.parent.mkdir(parents=True,exist_ok=True);"
            + "f=os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'ab',buffering=0);"
            + "r=subprocess.run("+repr([str(python),*arguments])+",stdin=subprocess.DEVNULL,stdout=f,stderr=f,creationflags=0x08000000);"
            + "f.close();raise SystemExit(r.returncode)")


def windows_xml(python, config_path, interval, *, user=None, argv=None,
                  description="knowbase local team statistics collector", persistent=False, entry_args=None,
                  execution_seconds=300, restart_on_failure=False, log_path=None):
    """Native Task Scheduler XML; no shell, embedded credentials, or extra runtime."""
    ET.register_namespace("", NS)
    def add(parent, name, text=None, **attrs):
        node=ET.SubElement(parent,"{"+NS+"}"+name,attrs)
        if text is not None:node.text=str(text)
        return node
    task=ET.Element("{"+NS+"}Task",{"version":"1.3"})
    registration=add(task,"RegistrationInfo")
    add(registration,"Description",description)
    triggers=add(task,"Triggers")
    periodic=add(triggers,"TimeTrigger")
    add(periodic,"StartBoundary",(datetime.now().astimezone()+timedelta(seconds=interval)).replace(microsecond=0).isoformat())
    add(periodic,"Enabled","true")
    repetition=add(periodic,"Repetition")
    add(repetition,"Interval",f"PT{interval}S")
    add(repetition,"StopAtDurationEnd","false")
    logon=add(triggers,"LogonTrigger");add(logon,"Enabled","true")
    username=user or ((os.environ.get("USERDOMAIN")+"\\") if os.environ.get("USERDOMAIN") else "")+getpass.getuser()
    add(logon,"UserId",username);add(logon,"Delay","PT1M")
    principals=add(task,"Principals");principal=add(principals,"Principal",id="Collector")
    add(principal,"UserId",username);add(principal,"LogonType","InteractiveToken");add(principal,"RunLevel","LeastPrivilege")
    settings=add(task,"Settings")
    for name,value in {"MultipleInstancesPolicy":"IgnoreNew","DisallowStartIfOnBatteries":"false",
                       "StopIfGoingOnBatteries":"false","StartWhenAvailable":"true","Enabled":"true",
                       "ExecutionTimeLimit":"PT0S" if persistent else 'PT5M' if execution_seconds==300 else f"PT{execution_seconds}S","AllowStartOnDemand":"true"}.items():
        add(settings,name,value)
    if persistent or restart_on_failure:
        restart=add(settings,'RestartOnFailure');add(restart,'Interval','PT1M');add(restart,'Count','3')
    actions=add(task,"Actions",Context="Collector");action=add(actions,"Exec")
    # Task XML has no environment block: set the explicit config before importing knowbase.
    loader="import os,runpy;os.environ['KNOWBASE_CONFIG']="+repr(str(config_path))+";runpy.run_module('knowbase',run_name='__main__',alter_sys=True)"
    arguments=entry_args if entry_args is not None else ["-I","-c",loader]+(argv or ["team","sync"])
    add(action,"Command",str(Path(python).with_name('pythonw.exe')))
    code=windows_launcher(python,arguments,log_path or Path(config_path).parent/'logs/background-task.log')
    add(action,"Arguments",subprocess.list2cmdline(['-I','-c',code]))
    return ET.tostring(task,encoding="utf-16",xml_declaration=True)


def _backup(root, files):
    if any(p.is_symlink() for p in files):raise ValueError("refusing symlink scheduler configuration")
    values={p:p.read_bytes() if p.exists() else None for p in files}
    directory=root/"worker-backups"/uuid.uuid4().hex
    directory.mkdir(parents=True,mode=0o700)
    for i,(p,raw) in enumerate(values.items()):
        if raw is not None:_atomic(directory/str(i),raw)
    return values


def _restore(values):
    for path,raw in values.items():
        if raw is None:path.unlink(missing_ok=True)
        else:_atomic(path,raw)


def _windows_snapshot(python, task_name=WINDOWS_TASK,
                      description="knowbase local team statistics collector"):
    """Do not overwrite an unknown task; capture the registered task, not a stale file."""
    listed=_execute(["schtasks","/Query","/FO","CSV","/NH"])
    raw=getattr(listed,"stdout",b"") or b""
    text=raw.decode("utf-8-sig",errors="replace") if isinstance(raw,bytes) else raw
    present=any(row and row[0].strip().lstrip("\\").casefold()==task_name.casefold() for row in csv.reader(io.StringIO(text)))
    if not present:return None
    queried=_execute(["schtasks","/Query","/TN",task_name,"/XML"])
    raw=getattr(queried,"stdout",b"") or b""
    try:task=ET.fromstring(raw)
    except (ValueError,ET.ParseError):raise RuntimeError("existing scheduler task cannot be verified") from None
    ns={"t":NS}
    command=task.find(".//t:Exec/t:Command",ns);description_node=task.find("t:RegistrationInfo/t:Description",ns)
    if (command is None or not isinstance(command.text,str) or command.text.casefold() not in {str(python).casefold(),str(Path(python).with_name('pythonw.exe')).casefold()} or description_node is None or
            description_node.text!=description):
        raise RuntimeError("existing task is not an owned knowbase task; configuration preserved")
    return ET.tostring(task,encoding="utf-16",xml_declaration=True)


def install(*, remove=False, label=LABEL, task_name=WINDOWS_TASK, argv=None,
            description="knowbase local team statistics collector",
            descriptor_name="worker-task.xml", persistent=False, entry_args=None, interval_seconds=None,
            execution_seconds=300, restart_on_failure=False):
    cfg=config.load_config();python=fixed_python()
    if not python.exists():raise ValueError("fixed knowbase runtime must be installed first")
    from .service import root_path
    root=root_path(cfg)
    interval=max(300,int(interval_seconds)) if interval_seconds is not None else max(900,int(cfg["team"].get("interval_seconds",900)))
    if interval>86400:raise ValueError("worker interval must be between 900 and 86400 seconds")
    if sys.platform=="darwin":
        path=Path.home()/f"Library/LaunchAgents/{label}.plist";target=f"gui/{os.getuid()}"
        old=_backup(root,[path])
        _execute(["launchctl","bootout",f"{target}/{label}"],required=False)
        if remove:
            path.unlink(missing_ok=True);return {"worker":"removed","platform":"launchagent"}
        value={"Label":label,"ProgramArguments":[str(python)]+(entry_args if entry_args is not None else ["-I","-m","knowbase"]+(argv or ["team","sync"])),
               "StartInterval":interval,"RunAtLoad":True,
               "EnvironmentVariables":{"KNOWBASE_CONFIG":str(config.CONFIG_PATH.resolve())},
               "StandardOutPath":str(root/"worker.log"),"StandardErrorPath":str(root/"worker-error.log")}
        if persistent:
            value.pop('StartInterval',None);value.update(KeepAlive=True,ThrottleInterval=5)
        elif restart_on_failure:value.update(KeepAlive={'SuccessfulExit':False},ThrottleInterval=30)
        try:
            _atomic(path,plistlib.dumps(value))
            _execute(["launchctl","bootstrap",target,str(path)])
        except (OSError,RuntimeError):
            _execute(["launchctl","bootout",f"{target}/{label}"],required=False)
            _restore(old)
            if old[path] is not None:_execute(["launchctl","bootstrap",target,str(path)],required=False)
            raise RuntimeError("LaunchAgent install failed; previous configuration restored") from None
        return {"worker":"installed","platform":"launchagent","interval_seconds":interval,"path":str(path)}
    if sys.platform.startswith("linux"):
        directory=Path.home()/".config/systemd/user";name=task_name
        files=[directory/f"{name}.service",directory/f"{name}.timer"]
        old=_backup(root,files)
        if remove:
            _execute(["systemctl","--user","disable","--now",name+".timer"],required=False)
            _execute(["systemctl","--user","disable","--now",name+".service"],required=False)
            for path in files:path.unlink(missing_ok=True)
            _execute(["systemctl","--user","daemon-reload"])
            return {"worker":"removed","platform":"systemd-user"}
        quote=lambda value:json.dumps(str(value).replace("%","%%"))
        command=" ".join(quote(arg) for arg in [python]+(entry_args if entry_args is not None else ["-I","-m","knowbase"]+(argv or ["team","sync"])))
        env=quote("KNOWBASE_CONFIG="+str(config.CONFIG_PATH.resolve()))
        try:
            if persistent:
                _execute(['systemctl','--user','disable','--now',name+'.timer'],required=False)
                files[1].unlink(missing_ok=True)
                _atomic(files[0],f"[Service]\nType=simple\nEnvironment={env}\nExecStart={command}\nRestart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n".encode())
            else:
                restart='Restart=on-failure\nRestartSec=30\n' if restart_on_failure else ''
                _atomic(files[0],f"[Service]\nType=oneshot\nEnvironment={env}\nExecStart={command}\nTimeoutStartSec={execution_seconds}\n{restart}".encode())
                _atomic(files[1],f"[Timer]\nOnBootSec=60\nOnUnitActiveSec={interval}\nPersistent=true\n[Install]\nWantedBy=timers.target\n".encode())
            _execute(["systemctl","--user","daemon-reload"])
            _execute(["systemctl","--user","enable","--now",name+('.service' if persistent else '.timer')])
        except (OSError,RuntimeError):
            _execute(["systemctl","--user","disable","--now",name+".timer"],required=False)
            if persistent:_execute(['systemctl','--user','disable','--now',name+'.service'],required=False)
            _restore(old);_execute(["systemctl","--user","daemon-reload"],required=False)
            if old[files[1]] is not None:_execute(["systemctl","--user","enable","--now",name+".timer"],required=False)
            elif old[files[0]] is not None:_execute(['systemctl','--user','enable','--now',name+'.service'],required=False)
            raise RuntimeError("systemd install failed; previous configuration restored") from None
        return {"worker":"installed","platform":"systemd-user","interval_seconds":interval}
    if sys.platform=="win32":
        if not remove and not python.with_name("pythonw.exe").is_file():raise ValueError("fixed runtime pythonw.exe is required for silent background tasks")
        path=root/descriptor_name;old=_backup(root,[path])
        registered=_windows_snapshot(python,task_name,description)
        if remove:
            if registered is not None:_execute(["schtasks","/Delete","/TN",task_name,"/F"])
            path.unlink(missing_ok=True);return {"worker":"removed","platform":"task-scheduler"}
        try:
            _atomic(path,windows_xml(python,config.CONFIG_PATH.resolve(),interval,argv=argv,description=description,persistent=persistent,
                                    entry_args=entry_args,execution_seconds=execution_seconds,restart_on_failure=restart_on_failure,log_path=root/(task_name+".log")))
            _execute(["schtasks","/Create","/TN",task_name,"/XML",str(path),"/F"])
            _execute(["schtasks","/Query","/TN",task_name])
            _execute(["schtasks","/Run","/TN",task_name])
        except (OSError,RuntimeError):
            _restore(old)
            if registered is not None:
                restored=root/"worker-backups"/uuid.uuid4().hex/"registered-task.xml"
                _atomic(restored,registered)
                _execute(["schtasks","/Create","/TN",task_name,"/XML",str(restored),"/F"],required=False)
            else:_execute(["schtasks","/Delete","/TN",task_name,"/F"],required=False)
            raise RuntimeError("Task Scheduler install failed; previous configuration restored") from None
        return {"worker":"installed","platform":"task-scheduler","interval_seconds":interval,"path":str(path),"user_session_required":True}
    raise ValueError("unsupported scheduler platform")
