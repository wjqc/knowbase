"""Recover only explicit Hook transcript references after maintenance, never prompts."""
import json
import os
from pathlib import Path

from .. import config, jobs


def recover_hooks(repo, conn, cfg):
    root = Path(os.environ.get('KNOWBASE_RUNTIME_ROOT', str(Path.home()/'.knowbase/runtime')))
    if (root/'update-drain.json').exists():
        return {'recovered': 0, 'gaps': 0}
    recovered = gaps = 0
    for path in sorted((root/'deferred-hooks').glob('*.json'))[:100]:
        if path.is_symlink() or path.stat().st_size > 16384:
            continue
        try:
            data = json.loads(path.read_text())
            if Path(data.get('config_path', '')).resolve() != config.CONFIG_PATH.resolve():
                continue  # A different store must recover its own events.
            host = cfg.get('inflight', {}).get('host_profile') or data.get('host', 'unknown')
            session = data.get('session_id', '')
            row = conn.execute('SELECT scope FROM context_sessions WHERE host_profile=? AND session_id=?',
                               (host, session)).fetchone()
            scope = row[0] if row else ''
            transcript = data.get('transcript_path', '')
            result = None
            if scope and transcript and Path(transcript).is_file():
                from ..adapters.host_events import capture_increment
                result = capture_increment(repo, session, transcript, scope, host, automatic=True)
            if result is not None and not result.get('error'):
                recovered += 1
            else:
                # No transcript means a real observation gap, even if older observations exist.
                cursor = 'update-hook:'+path.stem
                if not conn.execute('SELECT 1 FROM capture_gaps WHERE next_cursor=?', (cursor,)).fetchone():
                    gap = jobs.record_capture_gap(conn, scope, cursor=cursor, note='program_update_capture_gap')
                    if gap.get('error'):
                        continue
                gaps += 1
            path.unlink()
        except (OSError, ValueError, TypeError):
            continue
    return {'recovered': recovered, 'gaps': gaps}
