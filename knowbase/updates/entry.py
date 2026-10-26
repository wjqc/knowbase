"""Stable Hook/CLI entry; no application imports until the update barrier is clear."""
import json
import os
import re
import runpy
import sys
import time
import uuid
from pathlib import Path


def _main(root):
    if (root/'update-drain.json').exists():
        if len(sys.argv)>2 and sys.argv[1]=='hook':
            try:payload=json.loads(sys.stdin.read(1024*1024))
            except ValueError:payload={}
            session=str(payload.get('session_id') or payload.get('sessionId') or '')
            record={'event':sys.argv[2],'config_path':os.environ.get('KNOWBASE_CONFIG',str(Path.home()/'.knowbase/config.json')),
                    'host':sys.argv[sys.argv.index('--style')+1] if '--style' in sys.argv else 'unknown',
                    'at':time.time()}
            if re.fullmatch(r'[A-Za-z0-9:_-]{1,160}',session):record['session_id']=session
            for key,aliases in [('cwd',['cwd','project_dir']),('transcript_path',['transcript_path','transcriptPath'])]:
                value=next((payload.get(a) for a in aliases if payload.get(a)),None)
                if isinstance(value,str) and len(value)<=2048 and '\n' not in value and Path(value).is_absolute():record[key]=value
            directory=root/'deferred-hooks';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            path=directory/(uuid.uuid4().hex+'.json')
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:json.dump(record,f)
            return 0  # Never keep the user's task alive for maintenance.
        print('knowbase is updating; retry after the update completes',file=sys.stderr);return 75
    sys.argv[0]='knowbase'
    runpy.run_module('knowbase',run_name='__main__',alter_sys=True)
    return 0


def main():
    root=Path(os.environ.get('KNOWBASE_RUNTIME_ROOT',str(Path.home()/'.knowbase/runtime')))
    directory=root/'entries';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(str(os.getpid())+'-'+uuid.uuid4().hex+'.json')
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'pid':os.getpid(),'started_at':time.time()},f)
    try:return _main(root)
    finally:path.unlink(missing_ok=True)


if __name__=='__main__':raise SystemExit(main())
