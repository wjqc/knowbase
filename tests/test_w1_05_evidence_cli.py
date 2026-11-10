"""W1-5：证据包 CLI 接线（keygen / evidence-export / evidence-import）。

覆盖：keygen 幂等（重跑读回同公钥同 key_id，私钥 0600）；导出包 schema/
签名/事件计数；导入侧信任语义——registry 含己公钥 → imported；伪签/未登记
→ unverified 零写库；import 后自动链接治理重算；registry 缺失时显式报错
指向 ENROLLMENT.md。

HOME 隔离：keygen/export 的密钥路径走 Path.home()，测试全程 monkeypatch
HOME 到 tmp，绝不触碰真实 ~/.knowbase/team-keys。
"""
import json
import os
import stat
from pathlib import Path

from knowbase import __main__ as cli, config, index, writer
from knowbase.knowledge_lifecycle import attribution, trust, validators
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision
from knowbase import store

BODY = ("## 结论\n证据可跨机验签。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/ev.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")

PASS_REPORT = """\
=========================== short test summary info ============================
PASSED tests/test_ev.py::test_rule
========================= 1 passed in 0.01s ==================
"""


def _seed_l3(conn, repo, mid):
    meta, body, _ = store.load(repo, mid)
    revision = _compute_revision(meta, body)
    report = validators.parse_pytest_text(
        validators.stamp_report_text(PASS_REPORT, memory_id=mid, content_revision=revision,
                                     validator_id="pytest-text"))
    attribution.record_execution(conn, memory_id=mid, content_revision=revision,
                                 artifact_hash=report["artifact_hash"], validator_id="pytest-text")
    report["target"] = {"memory_id": mid,
                        "content_revision": revision}
    result = attribution.verify_execution(conn, repo, run_id="",
                                          validator_id="pytest-text", report=report)
    assert not result.get("error"), result


def _registry_for(public_pem: str, key_id: str, installation_id: str) -> dict:
    # scopes 须覆盖证据所属 scope（本测试卡在 orders）。
    return {"issuers": {installation_id: {"public_pem": public_pem,
                                          "scopes": ["knowbase", "global", "orders"],
                                          "role": "member"}},
            "key_epochs": {key_id: {"public_pem": public_pem, "revoked": False}}}


def test_keygen_idempotent_and_private_permissions(memory_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["lifecycle", "keygen"]) == 0
    first = json.loads(capsys.readouterr().out)
    keys_dir = tmp_path / ".knowbase" / "team-keys"
    priv = keys_dir / "device-ed25519-private.pem"
    pub = keys_dir / "device-ed25519-public.pem"
    assert priv.is_file() and pub.is_file()
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600
    assert first["generated"] is True and first["key_id"].startswith("key-")
    assert trust.key_id_for_public(first["public_pem"]) == first["key_id"]
    # 重跑：读回同一密钥（幂等，generated=False）。
    assert cli.main(["lifecycle", "keygen"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["generated"] is False and again["key_id"] == first["key_id"]


def test_export_import_roundtrip_trust_semantics(memory_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    card = writer.create(memory_repo, "bizrule", "跨机规则", BODY, scope="orders",
                         provenance="PRD-7")
    assert not card.get("error"), card
    mid = card["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    _seed_l3(conn, memory_repo, mid)
    conn.close()
    assert cli.main(["lifecycle", "keygen"]) == 0
    keygen = json.loads(capsys.readouterr().out)
    # 导出：签名包落盘。
    pkg_path = tmp_path / "pkg.json"
    assert cli.main(["lifecycle", "evidence-export", "--output", str(pkg_path)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["events"] == 1 and exported["key_id"] == keygen["key_id"]
    package = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert package["schema"] == "knowbase.lifecycle.evidence-package.v1"
    envelope = package["events"][0]
    assert envelope["signature"] and envelope["key_id"] == keygen["key_id"]
    assert trust.verify_envelope(envelope, keygen["public_pem"])
    # 接收侧：registry 含己公钥 → 导入成功（第二设备语义：新库 + 同 repo 卡）。
    registry = _registry_for(keygen["public_pem"], keygen["key_id"],
                             package["issuer_installation_id"])
    reg_path = tmp_path / "trust-registry.json"
    reg_path.write_text(json.dumps(registry), encoding="utf-8")
    import sqlite3
    peer_db = tmp_path / "peer-memory.db"
    peer_conn = sqlite3.connect(peer_db)
    apply_all(peer_conn)
    from knowbase.knowledge_lifecycle import evidence as lc_evidence
    stats = lc_evidence.import_package(peer_conn, package, registry=registry)
    assert stats["imported"] == 1 and not stats["unverified"], stats
    # 伪签/未登记（换一对密钥的 registry）→ unverified 零写库。
    stranger = trust.generate_keypair()
    bad_registry = _registry_for(stranger["public_pem"],
                                 trust.key_id_for_public(stranger["public_pem"]),
                                 package["issuer_installation_id"])
    peer2 = sqlite3.connect(tmp_path / "peer2.db")
    apply_all(peer2)
    stats2 = lc_evidence.import_package(peer2, package, registry=bad_registry)
    assert stats2["unverified"] == 1 and stats2["imported"] == 0, stats2
    assert peer2.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0] == 0
    peer_conn.close()
    peer2.close()


def test_import_cli_missing_registry_points_to_enrollment(memory_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    pkg_path = tmp_path / "pkg.json"
    pkg_path.write_text(json.dumps(
        {"schema": "knowbase.lifecycle.evidence-package.v1", "team_id": "default",
         "issuer_installation_id": "INST-x", "generated_at": "t", "events": []}),
        encoding="utf-8")
    rc = cli.main(["lifecycle", "evidence-import", str(pkg_path),
                   "--registry", str(tmp_path / "nope.json")])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "REGISTRY_NOT_FOUND" and "ENROLLMENT.md" in out["detail"]
    assert cli.main(["lifecycle", "evidence-export",
                     "--output", str(tmp_path / "o.json")]) == 1  # 无密钥：先 keygen
    assert json.loads(capsys.readouterr().out)["error"] == "PRIVATE_KEY_NOT_FOUND"
