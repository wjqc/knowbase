"""Stable stdlib MCP transport; installed outside the replaceable application package."""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
    with temp.open('x') as f:
        os.chmod(temp,0o600);json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def run(backend_command=None):
    root=Path(os.environ.get('KNOWBASE_RUNTIME_ROOT',str(Path.home()/'.knowbase/runtime')))
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    registry=root/'bridges'/f'{os.getpid()}.json';drain=root/'update-drain.json'
    incoming=queue.Queue(maxsize=512);backend=None;generation=0;pending=set();reverse=set();waiting=[]
    initial=None;negotiated=None;initial_build=None;current_build=None;replaying=False;last_restart=0
    last_registry=None;last_registry_write=0

    def read(stream,kind,gen=0):
        try:
            for line in stream:
                if len(line)>16*1024*1024:break
                incoming.put((kind,gen,line))
        finally:incoming.put((kind+'-eof',gen,''))

    def output(value):
        sys.stdout.write(json.dumps(value,ensure_ascii=False)+'\n');sys.stdout.flush()

    def send(value):
        backend.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');backend.stdin.flush()

    def error(request_id,reason):
        output({'jsonrpc':'2.0','id':request_id,'error':{'code':-32001,'message':reason,
                'data':{'retryable':True}}})

    def stop():
        nonlocal backend
        if backend is None:return
        try:
            backend.terminate();backend.wait(timeout=5)
        except (OSError,subprocess.TimeoutExpired):
            backend.kill();backend.wait(timeout=5)
        backend=None

    def start():
        nonlocal backend,generation,replaying,last_restart
        generation+=1;last_restart=time.monotonic()
        backend=subprocess.Popen(backend_command or [sys.executable,'-I','-m','knowbase','serve'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr,text=True,bufsize=1)
        threading.Thread(target=read,args=(backend.stdout,'backend',generation),daemon=True).start()
        if initial:
            replaying=True
            send({**initial,'id':'knowbase-internal-initialize-'+str(generation)})

    threading.Thread(target=read,args=(sys.stdin,'host'),daemon=True).start()
    start()
    try:
        while True:
            paused=drain.exists()
            try:
                marker=json.loads(drain.read_text()) if paused else {}
                stop_backend=paused and isinstance(marker,dict) and marker.get('phase','pause_backends')=='pause_backends'
            except (OSError,ValueError):stop_backend=False
            if paused and stop_backend and not pending and not reverse:
                stop();replaying=False
            elif not paused and backend is None and time.monotonic()-last_restart>=.5:
                start()
            state={'pid':os.getpid(),'backend_pid':backend.pid if backend else None,
                'phase':'paused' if paused and backend is None else 'draining' if paused else 'running',
                'active_requests':len(pending)+len(reverse),
                'config_path':os.environ.get('KNOWBASE_CONFIG',str(Path.home()/'.knowbase/config.json')),
                'initial_build':initial_build,'current_build':current_build,'protocol_version':negotiated}
            if state!=last_registry or time.monotonic()-last_registry_write>=2:
                atomic(registry,{**state,'updated_at':time.time()});last_registry=state;last_registry_write=time.monotonic()
            if not paused and backend is not None and not replaying:
                for message,received_at in waiting:
                    if time.monotonic()-received_at>300:
                        if 'id' in message:error(message['id'],'knowbase update wait expired; retry the operation')
                    else:
                        send(message)
                        if 'method' in message and 'id' in message:pending.add(message['id'])
                waiting=[]
            try:kind,gen,line=incoming.get(timeout=.2)
            except queue.Empty:continue
            if kind=='host-eof':break
            if kind=='backend-eof':
                if gen!=generation:continue
                for request_id in pending:error(request_id,'knowbase backend disconnected; check idempotent receipt before retry')
                pending.clear();reverse.clear();stop();replaying=False;continue
            try:message=json.loads(line)
            except ValueError:continue
            if not isinstance(message,dict):continue
            if kind=='host':
                paused=drain.exists()
                if message.get('method')=='initialize':initial=message
                if 'method' not in message and 'id' in message:
                    reverse.discard(message['id'])
                    if backend is not None:send(message)
                elif message.get('method')=='notifications/cancelled' and backend is not None:
                    send(message)
                elif paused or backend is None or replaying:
                    if len(waiting)>=256:
                        if 'id' in message:error(message['id'],'knowbase update queue is full; retry later')
                    else:waiting.append((message,time.monotonic()))
                else:
                    send(message)
                    if 'method' in message and 'id' in message:pending.add(message['id'])
            elif gen==generation:
                internal=message.get('id')=='knowbase-internal-initialize-'+str(generation)
                if internal or initial and message.get('id')==initial.get('id') and 'result' in message:
                    result=message.get('result',{})
                    if internal and (message.get('error') or result.get('protocolVersion')!=negotiated):
                        for item,_ in waiting:
                            if 'id' in item:error(item['id'],'updated backend protocol is incompatible')
                        waiting=[];stop();replaying=False;last_restart=time.monotonic()+30;continue
                    negotiated=result.get('protocolVersion')
                    found=re.search(r'\[knowbase-build:([a-f0-9]+)\]',result.get('instructions',''))
                    current_build=found.group(1) if found else None
                    if initial_build is None:initial_build=current_build
                    if internal:
                        send({'jsonrpc':'2.0','method':'notifications/initialized'});replaying=False
                        output({'jsonrpc':'2.0','method':'notifications/tools/list_changed'});continue
                    result.setdefault('capabilities',{}).setdefault('tools',{})['listChanged']=True
                if 'method' in message and 'id' in message:reverse.add(message['id'])
                elif 'id' in message:pending.discard(message['id'])
                output(message)
    finally:
        stop();registry.unlink(missing_ok=True)


if __name__=='__main__':run()
