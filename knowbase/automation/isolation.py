"""Run synthetic probes in a disposable process/config/repository, never the user's store."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def isolated_probe(executor_config, *, scope):
    from ..gitops import _kill_tree
    from ..credentials import transform
    from .receipts import fingerprint
    before=fingerprint(executor_config)

    with tempfile.TemporaryDirectory(prefix="knowbase-executor-probe-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        repo, workspace, cp = root / "memory", root / "workspace", root / "config.json"
        workspace.mkdir()
        cp.write_text(json.dumps({
            "repo_path": str(repo), "knowledge_path": "",
            "git": {"auto_pull": False, "auto_push": False, "auto_commit": False},
            "embedding": {"enabled": False}, "value": {"events_enabled": False},
            "team": {"enabled": False}, "automation": {"executors": []},
        }))
        cp.chmod(0o600)
        env = {**os.environ, "KNOWBASE_CONFIG": str(cp), "KNOWBASE_REPO_PATH": str(repo),
               "KNOWBASE_AGENT_NAME": "knowbase-isolation-probe"}
        # -I plus an explicit package root pins the same source/build as the caller.
        package_root = Path(__file__).resolve().parents[2]
        bootstrap = ("import sys; sys.path.insert(0,sys.argv.pop(1)); "
                     "from knowbase.automation.isolation import child_main; child_main()")
        options = ({"start_new_session": True} if os.name != "nt" else
                   {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW})
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", bootstrap, str(package_root)],
            cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, **options)
        request = json.dumps({"executor": executor_config, "scope": scope,
                              "workspace": str(workspace)}, ensure_ascii=False)
        try:
            out, _ = process.communicate(request, timeout=max(30, float(executor_config.get("timeout_seconds") or 120) + 30))
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return {"passed": False, "failure_reasons": ["timeout"]}
        finally:
            # A CLI can leave helper processes behind even after its main process exits
            # (Windows included: taskkill on an exited PID is tolerated).
            _kill_tree(process)
        if process.returncode:
            return {"passed": False, "failure_reasons": ["probe_process_failed"]}
        try:
            result = json.loads(out)
        except ValueError:
            return {"passed": False, "failure_reasons": ["probe_report_invalid"]}
        if not isinstance(result,dict):return {'passed':False,'failure_reasons':['probe_report_invalid']}
        if fingerprint(executor_config)!=before:
            return {'passed':False,'failure_reasons':['executor_configuration_changed']}
        result['config_hash']=before
        return transform(result)


def child_main():
    import io
    from contextlib import redirect_stdout
    from .. import config
    from ..__main__ import cmd_init
    from .execution import make_runner
    from .protocol import run_isolation_probe
    from ..credentials import transform

    try:
        request = json.load(sys.stdin)
        with redirect_stdout(io.StringIO()):
            if cmd_init():
                raise RuntimeError("probe_init_failed")
            runner = make_runner({**request["executor"],
                                             "working_directory": request["workspace"]})
            result = run_isolation_probe(runner, repo=config.repo_path(), scope=request["scope"])
    except Exception:
        result = {"passed": False, "failure_reasons": ["probe_process_failed"]}
    print(json.dumps(transform(result), ensure_ascii=False))
