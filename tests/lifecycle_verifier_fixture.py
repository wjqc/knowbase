import hashlib
import json
import sys
from pathlib import Path

from knowbase import config, store
from knowbase.knowledge_lifecycle import attribution, validators


def configure_verifier(repo, mid, revision, *, validator_id="pytest-text", group="primary",
                       case="default", outcome="pass", project="repo:orders", auto=False, workspace=None):
    cwd = repo.parent / f"verifier-{validator_id}-{case}"
    cwd.mkdir(exist_ok=True)
    test_file = cwd / "test_claim.py"
    test_file.write_text("from pathlib import Path\n\ndef test_claim():\n"
                         "    assert Path('input.txt').read_text().startswith('valid:')\n")
    (cwd / "input.txt").write_text(f"{'valid' if outcome == 'pass' else 'invalid'}:{case}")
    spec = {
        "memory_id": mid, "content_revision": revision, "cwd": str(cwd),
        "argv": [sys.executable, "-I", "-m", "pytest", "-rA", "-p", "no:cacheprovider", "test_claim.py"],
        "claim_map": {"test_claim.py::test_claim": "C-whole"},
        "verifier_files": {"test_claim.py": hashlib.sha256(test_file.read_bytes()).hexdigest()},
        "input_paths": ["input.txt"], "project_key": project, "independence_group": group, "auto": auto,
        "workspace": str(workspace) if workspace else "",
    }
    cfg_path = Path(config.CONFIG_PATH)
    cfg = json.loads(cfg_path.read_text())
    entries = cfg.setdefault("knowledge_lifecycle", {}).setdefault("validators", [])
    entry = next((item for item in entries if item["validator_id"] == validator_id), None)
    if entry is None:
        entry = {"validator_id": validator_id, "kind": "pytest"}
        entries.append(entry)
    entry["bindings"] = [b for b in entry.get("bindings", []) if b["memory_id"] != mid] + [spec]
    cfg_path.write_text(json.dumps(cfg))
    meta, body, _ = store.load(repo, mid)
    return validators.binding_for(validator_id, meta, body, revision)


def run_verifier(conn, repo, mid, revision, *, validator_id="pytest-text", run_id="", **kwargs):
    binding = configure_verifier(repo, mid, revision, validator_id=validator_id, **kwargs)
    return attribution.run_verification(conn, repo, memory_id=mid, revision=revision,
                                        validator_id=validator_id, binding=binding, run_id=run_id)
