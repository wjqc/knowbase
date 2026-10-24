"""Content build marker; never assumes an installed version string identifies source."""
import hashlib
from pathlib import Path


def build_id():
    root=Path(__file__).parent
    h=hashlib.sha256()
    files=sorted(p for p in root.rglob("*") if p.is_file() and (p.suffix==".py" or p.parent==root/"data" and p.suffix==".json"))
    for path in files:
        h.update(str(path.relative_to(root)).encode());h.update(path.read_bytes())
    return h.hexdigest()[:16]
