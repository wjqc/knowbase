"""Local, explicit directory subscriptions and foreground hash polling.

No background service is installed. Polling deliberately calls the full hash-based
sync: filesystem timestamps/events cannot reliably detect same-mtime changes.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

from .document_library import DocumentLibrary, _now


class DirectoryWatch:
    def __init__(self, library: DocumentLibrary):
        self.library = library

    @staticmethod
    def validate_interval(interval):
        interval = float(interval)
        if not math.isfinite(interval) or not 1 <= interval <= 3600:
            raise ValueError("watch interval must be between 1 and 3600 seconds")
        return interval

    def register(self, directory, *, scope, exclude=(), interval=10):
        raw = Path(directory).expanduser()
        if raw.is_symlink():
            raise ValueError("watch root cannot be a symlink")
        root = raw.resolve()
        if not root.is_dir():
            raise ValueError("watch root must be an existing directory")
        if root.is_relative_to(self.library.repo) or self.library.repo.is_relative_to(root):
            raise ValueError("watch directory and library must not contain each other")
        scope = self.library.validate_scope(scope)
        interval = self.validate_interval(interval)
        exclude = list(exclude)
        if len(exclude) > 100 or any(not isinstance(p, str) or len(p) > 1000 for p in exclude):
            raise ValueError("at most 100 exclusion patterns of at most 1000 characters")
        identity = hashlib.sha256((str(root) + '\0' + scope).encode()).hexdigest()[:24]
        with self.library._connect() as conn:
            conn.execute("""INSERT INTO source_watch_roots(id,directory,scope,excludes,interval)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                excludes=excluded.excludes,interval=excluded.interval""",
                (identity, str(root), scope, json.dumps(exclude), interval))
        return next(r for r in self.roots() if r['id'] == identity)

    def roots(self):
        with self.library._connect() as conn:
            rows = conn.execute("SELECT * FROM source_watch_roots ORDER BY directory,scope").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['exclude'] = json.loads(item.pop('excludes'))
            item['last_result'] = json.loads(item['last_result']) if item['last_result'] else None
            result.append(item)
        return result

    def remove(self, identity):
        with self.library._connect() as conn:
            deleted = conn.execute("DELETE FROM source_watch_roots WHERE id=?", (identity,)).rowcount
        return {'removed': bool(deleted), 'id': identity, 'snapshots_retained': True}

    def sync_root(self, root, *, retries=2, stop=None, emit=None):
        """Retry transient sync errors; persist latest result. Never delete snapshots.

        Cancellation between attempts is cooperative. Ctrl-C may interrupt an active
        parser; SQLite transactions roll back, content-addressed snapshots may remain.
        """
        stop = stop or threading.Event()
        emit = emit or (lambda event: None)
        if not 0 <= retries <= 5:
            raise ValueError("retries must be between 0 and 5")
        def removed():
            if root['id'] == 'transient':
                return False
            with self.library._connect() as conn:
                return conn.execute('SELECT 1 FROM source_watch_roots WHERE id=?', (root['id'],)).fetchone() is None

        def cancelled():
            return stop.is_set() or removed()

        result = {'cancelled': True, 'errors': []}
        for attempt in range(retries + 1):
            if cancelled():
                result = {'cancelled': True, 'unregistered': removed(), 'errors': []}
                break
            started = time.monotonic()
            with self.library._connect() as conn:
                conn.execute("UPDATE source_watch_roots SET last_started=? WHERE id=?", (_now(), root['id']))
            emit({'event': 'sync_started', 'id': root['id'], 'directory': root['directory'], 'attempt': attempt + 1})
            try:
                directory = Path(root['directory']).expanduser()
                if directory.is_symlink() or (root['id'] != 'transient' and str(directory.resolve()) != root['directory']):
                    raise ValueError('registered root changed or became a symlink')
                result = self.library.sync(directory, scope=root['scope'], exclude=root['exclude'],
                    cancelled=cancelled, progress=lambda state: emit(dict(state, event='sync_progress', id=root['id'])))
            except KeyboardInterrupt:
                with self.library._connect() as conn:
                    conn.execute("UPDATE source_watch_roots SET last_finished=?,last_result=? WHERE id=?",
                                 (_now(), json.dumps({'cancelled': True}), root['id']))
                if removed():
                    result = {'cancelled': True, 'unregistered': True, 'errors': []}
                    emit({'event': 'sync_cancelled', 'id': root['id'], 'reason': 'unregistered'})
                    return result
                raise
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                result = {'errors': [{'error': str(exc)}]}
            result = dict(result, elapsed_seconds=round(time.monotonic() - started, 4))
            with self.library._connect() as conn:
                conn.execute("UPDATE source_watch_roots SET last_finished=?,last_result=? WHERE id=?",
                             (_now(), json.dumps(result, ensure_ascii=False), root['id']))
            emit({'event': 'sync_finished', 'id': root['id'], 'attempt': attempt + 1, 'result': result})
            if not result.get('errors') or attempt == retries:
                break
            delay = min(2 ** attempt, 30)
            emit({'event': 'retry_wait', 'id': root['id'], 'seconds': delay})
            if removed() or stop.wait(delay) or removed():
                result = dict(result, cancelled=True)
                with self.library._connect() as conn:
                    conn.execute('UPDATE source_watch_roots SET last_finished=?,last_result=? WHERE id=?',
                                 (_now(), json.dumps(result, ensure_ascii=False), root['id']))
                break
        return result

    def run(self, *, once=False, retries=2, stop=None, emit=None):
        stop = stop or threading.Event()
        emit = emit or (lambda event: None)
        due = {}
        results = {}
        if not self.roots():
            raise ValueError("no registered directories; use documents register first")
        try:
            while not stop.is_set():
                roots = self.roots()  # observe register/remove changes without restart
                if not roots:
                    break
                for root in roots:
                    if stop.is_set():
                        break
                    if once or time.monotonic() >= due.get(root['id'], 0):
                        results[root['id']] = self.sync_root(root, retries=retries, stop=stop, emit=emit)
                        due[root['id']] = time.monotonic() + root['interval']
                if once:
                    break
                stop.wait(min(1, max(0.01, min(due.get(r['id'], 0) for r in roots) - time.monotonic())))
        except KeyboardInterrupt:
            stop.set()
            emit({'event': 'cancelled', 'snapshots_retained': True})
        return {'cancelled': stop.is_set(), 'results': results}
