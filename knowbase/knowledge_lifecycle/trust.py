"""事件签名与受信登记（V2-13）：成熟 cryptography Ed25519，不自研密码协议。

签名覆盖：schema + event_id + 信封哈希（envelope_hash 已含 issuer/team/
scope/subject/payload 与 key_id）。验签失败/密钥不匹配/未登记签发者 →
事件不进治理（导入方仅计数并记 event_id，不入库）。

设备密钥由一次性登记（本机私有配置 + 独立 Git 信任分支）；key_id 轮换保留
旧公钥。部署时锁定已验证发布版本（依赖引导走 updates bootstrap 门）。
"""
import base64
import json
from pathlib import Path

from . import contracts

SIGNATURE_ALGO = "Ed25519"


def _ed25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


def generate_keypair() -> dict:
    """生成设备密钥对（PEM，私钥永不入事件包/知识 Git）。"""
    Ed25519PrivateKey, _pub, serialization = _ed25519()
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {"private_pem": private_pem, "public_pem": public_pem}


def sign_envelope(envelope: dict, private_pem: str, key_id: str) -> dict:
    """对信封签名：覆盖 schema、event_id 与完整信封哈希（去 signature 字段）。

    历史未签名事件不能事后补签沿用原 event_id/hash——补签需重新核验后
    以新事件引用原事件（reissue），不在本函数处理。"""
    Ed25519PrivateKey, _pub, _ser = _ed25519()
    key = Ed25519PrivateKey.from_private_bytes(
        _load_pem_private(private_pem))
    envelope = {k: v for k, v in envelope.items() if k not in ("signature", "signature_algo")}
    envelope["key_id"] = key_id
    message = (envelope["schema"] + "|" + envelope["event_id"] + "|"
               + contracts.envelope_hash(envelope)).encode("utf-8")
    signature = key.sign(message)
    envelope["signature"] = base64.b64encode(signature).decode()
    envelope["signature_algo"] = SIGNATURE_ALGO
    return envelope


def verify_envelope(envelope: dict, public_pem: str) -> bool:
    Ed25519PublicKey_cls = _ed25519()[1]
    try:
        key = Ed25519PublicKey_cls.from_public_bytes(_load_pem_public(public_pem))
        message = (envelope["schema"] + "|" + envelope["event_id"] + "|"
                   + contracts.envelope_hash(envelope)).encode("utf-8")
        signature = base64.b64decode(envelope.get("signature", ""))
        key.verify(signature, message)
        return envelope.get("signature_algo") == SIGNATURE_ALGO
    except Exception:
        return False


def _load_pem_private(private_pem: str) -> bytes:
    _priv, _pub, serialization = _ed25519()
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _load_pem_public(public_pem: str) -> bytes:
    _priv, _pub, serialization = _ed25519()
    key = serialization.load_pem_public_key(public_pem.encode())
    return key.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)


def load_trust_registry(path: Path) -> dict:
    """团队信任名单（维护者签发的版本化 policy，公钥/角色/scope）。

    格式：{"issuers": {issuer_installation_id: {"public_pem","scopes":[],"role"}},
          "key_epochs": {key_id: {"public_pem","revoked":bool}}}。本地可收紧，
    不能自行扩大团队授权。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"issuers": {}, "key_epochs": {}}
    except (OSError, ValueError):
        return {"issuers": {}, "key_epochs": {}}


def check_issuer(registry: dict, envelope: dict, scope: str) -> str | None:
    """返回信任结论：trusted / unknown_issuer / key_revoked / scope_not_granted。"""
    issuer = envelope.get("issuer_installation_id", "")
    entry = (registry.get("issuers") or {}).get(issuer)
    key_id = envelope.get("key_id", "")
    epoch = (registry.get("key_epochs") or {}).get(key_id)
    if entry is None:
        return "unknown_issuer"
    if epoch is None:
        return "unknown_key"
    if epoch.get("revoked"):
        return "key_revoked"
    if scope not in (entry.get("scopes") or []):
        return "scope_not_granted"
    return "trusted"
