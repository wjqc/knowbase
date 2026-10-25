"""Text inference sees disposable data/config paths; writes remain in the controller."""
import json
import os
import tempfile
from pathlib import Path

from .protocol import ExecutorFailure


def make_runner(options):
    if options.get('kind','command') == 'intranet_text':
        from .text import IntranetTextRunner
        return IntranetTextRunner(options)
    from .executor import CommandExecutorRunner
    return CommandExecutorRunner(options)


def execute_text(options, request, text, segments, policy):
    with tempfile.TemporaryDirectory(prefix='knowbase-inference-') as directory:
        root = Path(directory); root.chmod(0o700)
        cp = root/'config.json'; repo = root/'memory'
        cp.write_text(json.dumps({'repo_path':str(repo),'knowledge_path':'',
            'git':{'auto_commit':False,'auto_push':False,'auto_pull':False},
            'embedding':{'enabled':False},'team':{'enabled':False},'value':{'events_enabled':False}}));cp.chmod(0o600)
        seconds = max(.05,min(120,float(policy.get('batch_seconds',120)),float(options.get('timeout_seconds',120))))
        runner = make_runner({**options,'timeout_seconds':seconds,'working_directory':str(root)})
        runner.execution_env = {**os.environ,'KNOWBASE_CONFIG':str(cp),'KNOWBASE_REPO_PATH':str(repo),
                                'KNOWBASE_AGENT_NAME':'knowbase-background-distiller'}
        try:
            return runner.run_text(request,text,segments)
        except ExecutorFailure:
            raise
        except Exception:
            raise ExecutorFailure('executor_error') from None
