"""Private, expiring executor capability receipts; never a model-quality certificate."""
import json
import os
import time
from pathlib import Path

from .. import config
from ..embedding_provider import digest

TRANSIENT_ERRORS = frozenset({'timeout','rate_limited','rate_limit','process_exited','executor_error',
                             'auth_error','auth_missing','permission_client_unavailable'})


def fingerprint(options):
    from .executor import _resolve_binary
    binary = _resolve_binary(options.get('command')) if options.get('kind', 'command') == 'command' else None
    stamp = None
    if binary:
        stat = Path(binary).resolve().stat()
        stamp = [str(Path(binary).resolve()), stat.st_size, stat.st_mtime_ns]
    auth = options.get('auth_check') or {}
    ref = options.get('secret_ref', '')
    env_name = auth.get('env_var') or (ref[4:] if ref.startswith('env:') else None)
    auth_file = auth.get('file') or (ref[5:] if ref.startswith('file:') else None)
    file_stamp = None
    if auth_file:
        try:
            s = Path(auth_file).expanduser().stat(); file_stamp = [s.st_mtime_ns, s.st_size]
        except OSError:
            file_stamp = 'missing'
    return digest({'options': options, 'binary': stamp,
                   'auth_present': bool(env_name and env_name in os.environ), 'auth_file': file_stamp})


def _path(options):
    return config.CONFIG_PATH.parent/'executor-receipts'/('executor-'+digest(options.get('executor_id', options.get('command', '')))[:24]+'.json')


def record(options, report, *, version=None):
    path = _path(options)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('unsafe_receipt_path')
    current=fingerprint(options)
    try:previous=json.loads(path.read_text())
    except (OSError,ValueError):previous={}
    passed=bool(report.get('passed')) and report.get('config_hash',current)==current
    was_verified=(previous.get('config_hash')==current and
                  (previous.get('passed') is True or previous.get('had_verified') is True))
    data = {'schema': 'knowbase.executor-verification.v1', 'config_hash': current,
            'verified_at': time.time(), 'passed': passed, 'had_verified':passed or was_verified,
            'next_probe_at':0 if passed else time.time()+300,
            'version': version, 'usage_source': report.get('usage_source', 'unavailable')}
    temp = path.with_name(path.name+'.tmp-'+__import__('uuid').uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(data, f)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return data


def verified(options):
    path = _path(options)
    try:
        if path.is_symlink() or path.parent.is_symlink() or (os.name!='nt' and path.stat().st_mode & 0o077):
            return False
        data = json.loads(path.read_text())
        return (data.get('schema') == 'knowbase.executor-verification.v1'
                and data.get('passed') is True and data.get('config_hash') == fingerprint(options)
                and 0 <= time.time()-data.get('verified_at', 0) < 86400)
    except (OSError, ValueError, TypeError):
        return False


def needs_refresh(options):
    try:
        data=json.loads(_path(options).read_text())
        return (data.get('config_hash')==fingerprint(options) and
                ((data.get('passed') is True and time.time()-data.get('verified_at',0)>=86400)
                 or (data.get('passed') is False and data.get('had_verified') is True
                     and time.time()>=data.get('next_probe_at',float('inf')))))
    except (OSError,ValueError,TypeError):return False
