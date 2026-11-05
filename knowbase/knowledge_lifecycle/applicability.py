"""适用范围、失效感知与低频保护（V2-15）。

原则（设计 §12）：
- 依赖变化只生成 environment_changed 与待复验，不直接断言卡错；
  精确已知不匹配时限制该环境推荐，原环境仍有效则保留（条件分支不覆盖）。
- 时间衰减只表示"需复核"，不表示"错误"；importance=critical / keep_reason /
  未解冲突 / 关联活跃变更 / 正在复验的卡阻止自动归档。
- 复活 = 修订（新版本准入），不是旧卡翻回 verified。
"""
import json
import re
from datetime import datetime, timezone

from .. import store
from . import contracts

OBSERVED_EVENT = "environment.observed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(value: str):
    """宽松版本解析：`1.2.3` → (1,2,3)；不可解析返回 None（exact 语义）。"""
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value or "").strip())
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def _parse_spec(spec: str):
    """解析约束串（>=2,<3 / ==1.0 / 2.x 简写）为 [(op, version_tuple)]；
    不可解析返回 None（调用方按 exact/unknown 处理）。"""
    constraints = []
    for part in re.split(r",", str(spec or "").strip()):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(>=|<=|==|!=|>|<|=)?\s*v?([\d.]+(?:\.x)?)$", part)
        if not m:
            return None
        op = m.group(1) or "=="
        ver = m.group(2).rstrip(".")
        if ver.endswith(".x"):  # 2.x == >=2,<3
            base = ver[:-2]
            low = parse_version(base)
            high = list(low) if low else None
            if low is None or high is None:
                return None
            high[len(high) - 1 if len(high) > 1 else 0] += 1
            constraints.append((">=", low))
            constraints.append(("<", tuple(high)))
            continue
        parsed = parse_version(ver)
        if parsed is None:
            return None
        constraints.append((op if op != "=" else "==", parsed))
    return constraints or None


def _pad(a, b):
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def spec_matches(spec: str, version: str) -> bool | None:
    constraints = _parse_spec(spec)
    if constraints is None:
        parsed = parse_version(spec)
        observed = parse_version(version)
        if parsed is None or observed is None:
            return None  # 不可解析：exact/unknown 语义，不猜
        return parsed == observed
    observed = parse_version(version)
    if observed is None:
        return None
    for op, want in constraints:
        o, w = _pad(observed, want)
        if op == ">=" and not o >= w:
            return False
        if op == "<=" and not o <= w:
            return False
        if op == ">" and not o > w:
            return False
        if op == "<" and not o < w:
            return False
        if op in ("==", "!=") and (o != w) == (op == "=="):
            return False
    return True


def evaluate_card(applicability: dict, observed_components: dict) -> dict:
    """一卡适用范围 vs 观测环境：mismatch / match / unknown 分组件结论。

    mismatch ≠ 卡错：只限制该环境推荐并生成待复验；unknown 保留不动作。"""
    applicability = applicability or {}
    result = {"status": "match", "mismatched": [], "unknown": []}
    for comp in applicability.get("components") or []:
        if not isinstance(comp, dict):
            continue
        name = str(comp.get("name", ""))
        spec = str(comp.get("version_spec") or "")
        observed = observed_components.get(name)
        if observed is None:
            if spec:
                result["unknown"].append(name)  # 环境未观测该组件
            continue
        verdict = spec_matches(spec, observed) if spec else True
        if verdict is False:
            result["mismatched"].append({"name": name, "spec": spec, "observed": observed})
        elif verdict is None:
            result["unknown"].append(name)
    if result["mismatched"]:
        result["status"] = "mismatch"
    elif result["unknown"]:
        result["status"] = "unknown"
    return result


def observe_environment(conn, repo, project_key: str, components: dict,
                        *, source_artifact_hash: str = "") -> dict:
    """记录 environment.observed 并对适用范围冲突的卡生成待复验清单。

    只产生 revalidation 需求与（已 mismatch 时）环境限制标记，不置
    incorrect、不删内容。"""
    from .. import index
    event_id = contracts.stable_event_id(
        "environment-observer", f"{project_key}:{contracts.canonical_json(components)}",
        OBSERVED_EVENT, {}, OBSERVED_EVENT)
    conn.execute(
        "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,event_type,"
        "subject_revision,run_id,logical_task_key,independence_group,issuer_id,"
        "trust_basis,level,polarity,supersedes_event_id,revokes_event_id,payload_json,"
        "import_state,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, contracts.snapshot_hash(
            contracts.canonical_json(components).encode("utf-8")),
         OBSERVED_EVENT, "", "", "", "", "environment-observer", "deterministic-probe",
         "", "neutral", "", "",
         json.dumps({"project_key": project_key, "components": components,
                     "source_artifact_hash": source_artifact_hash},
                    ensure_ascii=False)[:4096], "local", _now()))
    revalidation = []
    restricted = []
    for meta, body, path in store.iter_all(repo, include_staging=True):
        applicability = meta.get("applicability")
        if not isinstance(applicability, dict):
            continue  # 无结构条件的老卡：unknown，不批量判过期
        verdict = evaluate_card(applicability, components)
        if verdict["status"] == "mismatch":
            revalidation.append({"memory_id": meta["id"], "detail": verdict["mismatched"],
                                 "reason": "environment_changed"})
            restricted.append(meta["id"])
    conn.commit()
    return {"event_id": event_id, "project_key": project_key,
            "revalidation_needed": revalidation, "restricted": restricted,
            "note": "mismatch restricts per-environment recommendation only; "
                    "original environments keep their evidence"}


def archive_protection(meta: dict, *, active_changes=0, unresolved_conflicts=0,
                       revalidating=False) -> bool:
    """低频重要卡保护：自动归档前必须检查（设计 §12）。"""
    if str(meta.get("importance", "")).lower() == "critical" or meta.get("keep_reason"):
        return True
    if active_changes or unresolved_conflicts or revalidating:
        return True
    if meta.get("revalidation_pending"):
        return True
    return False
