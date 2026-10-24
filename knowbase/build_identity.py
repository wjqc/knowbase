"""Content build marker; never assumes an installed version string identifies source."""
import hashlib
from pathlib import Path


def build_id():
    root=Path(__file__).parent
    h=hashlib.sha256()
    for name in ['index.py','context.py','embedding_provider.py','semantic_index.py','retrieval_state.py','integration.py']:
        p=root/name
        h.update(name.encode());h.update(p.read_bytes() if p.exists() else b'missing')
    return h.hexdigest()[:16]
