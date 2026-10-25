"""Rebuild a separate analysis DB from original v1 events; never guess stripped identity."""
import json
import os
import tempfile
from pathlib import Path

from .. import config
from . import aggregate, exchange


def rebuild(repo, output, packages=(), *, team_id='default'):
    repo=Path(repo).resolve();output=Path(output).expanduser().resolve()
    if output==repo or output.is_relative_to(repo):raise ValueError('analysis_db_must_be_outside_knowledge')
    # Exclusive creation protects the live aggregate and any unrelated file.
    fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    conn=None
    try:
        with tempfile.TemporaryDirectory(prefix='knowbase-original-events-') as td:
            local=Path(td)/'local-original.json'
            result=exchange.export_events(repo,local)
            if result.get('error'):raise ValueError('original_events_unavailable')
            local.chmod(0o600)
            inputs=[local]+[Path(p).expanduser() for p in packages]
            conn=aggregate.connect(output);counts={'accepted':0,'duplicate':0,'conflict':0,'rejected':0}
            for path in inputs:
                data=json.loads(path.read_text())
                if data.get('schema')!=exchange.PACKAGE_SCHEMA:
                    raise ValueError('raw_v1_packages_required_for_identity_recovery')
                receipt=exchange.import_package(conn,path,team_id=team_id)
                if receipt.get('error'):raise ValueError('original_package_invalid')
                for key in counts:counts[key]+=receipt['counts'].get(key,0)
            recovered=conn.execute("SELECT count(*) FROM agg_events WHERE member_binding_id<>'' AND session_id<>''").fetchone()[0]
            missing=conn.execute("SELECT count(*) FROM agg_events WHERE member_binding_id='' OR session_id=''").fetchone()[0]
            return {'output':str(output),'counts':counts,'events_with_member_and_session':recovered,
                    'identity_still_missing':missing,'original_outbox_and_receipts_unchanged':True}
    except Exception:
        if conn is not None:conn.close();conn=None
        for suffix in ('','-wal','-shm'):Path(str(output)+suffix).unlink(missing_ok=True)
        raise
    finally:
        if conn is not None:conn.close()
