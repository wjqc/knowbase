"""Fixed-revision inventory and single-publisher README marked-region updates."""
import hashlib
import re
from collections import Counter

import yaml

from .. import store
from .transport import TransportError

START = "<!-- knowbase:team-stats:start"
END = "<!-- knowbase:team-stats:end -->"


def inventory(git, revision):
    states = Counter({k: 0 for k in ("verified", "once", "staging", "stale", "archived", "unknown")})
    seen = set()
    for path in git.files(revision):
        if not path.endswith(".md") or path.split("/")[0] not in store.ALL_DIRS:
            continue
        raw = git.read(revision, path)
        if not raw or not raw.startswith("---\n"):
            continue
        try:
            end = raw.find("\n---", 3)
            if end < 0:
                raise ValueError("frontmatter is not closed")
            meta = yaml.safe_load(raw[3:end].strip())
        except (yaml.YAMLError, IndexError, ValueError):
            raise TransportError("shared knowledge metadata cannot be counted safely") from None
        if not isinstance(meta, dict) or not meta.get("id"):
            continue
        if meta["id"] in seen:
            raise TransportError("duplicate knowledge id in shared revision")
        seen.add(meta["id"])
        status = meta.get("status")
        key = status if status in {"stale", "archived"} else "staging" if path.startswith("staging/") else meta.get("confidence", "unknown")
        states[key if key in states else "unknown"] += 1
    return {"revision": revision, "total": sum(states.values()), "states": dict(states)}


def _fmt(value):
    return f"{value:,}" if value is not None else "未采集"


def markdown(snapshot):
    c = snapshot["coverage"]
    lines = ["### 团队知识库使用统计", "",
             f"更新：{snapshot['generated_at']} · 已收到 **{c['received']}/{c['registered']}** 名已登记成员。",
             "仅统计已接入设备的观测数据；未登记成员不在覆盖范围，当日数据持续补报。", "",
             f"调用 **{_fmt(snapshot['calls'])}** · 命中 **{_fmt(snapshot['hit_searches'])}** / 已知检索 {_fmt(snapshot['known_searches'])} · 更新 **{_fmt(snapshot['updates'])}**。",
             f"共享知识 **{_fmt(snapshot['knowledge_total'])}** 张（固定 revision：{(snapshot.get('inventory_revision') or '未知')[:12]}）。",
             f"统计窗口：{c['since'] or '未知'} 至 {c['watermark'] or '未知'}；库存按当前共享版本计一次。", "",
             "| 成员 | 上报状态 | 调用 | 命中 | 更新 | 请求 Token | 最后上报（UTC） |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    names = {"received": "已收到", "partial": "部分来源", "stale": "已过期", "missing": "未上报", "pending": "待接入", "recovery_required": "恢复待核对"}
    for m in snapshot["members"]:
        t = m["totals"]
        label = m["label"] + ("（已退出，保留历史）" if not m["current"] else "")
        lines.append(f"| {label} | {names[m['coverage']]} | {_fmt(t['calls'])} | {_fmt(t['hit_searches'])} | {_fmt(t['updates'])} | {_fmt(t['usage']['total'])} | {m['last_report'] or '未上报'} |")
    lines += ["", "历史 usage_log 与新调用不相加；请求 Token 仅为已采集来源。ZCode/Trae 用量未采集，Token 净节省待配对测量。",
              f"自动晋升 {_fmt(snapshot['evolution']['promoted'])} 次 · 自动失效 {_fmt(snapshot['evolution']['retired'])} 次；回执次数不等同于独立受益任务数。", ""]
    return "\n".join(lines)


def replace_region(original, content):
    body = "\n" + content.rstrip() + "\n"
    hash_value = hashlib.sha256(body.encode()).hexdigest()
    region = f"{START} sha256={hash_value} -->{body}{END}"
    if START not in original and END not in original:
        return original.rstrip() + "\n\n" + region + "\n"
    pattern = re.compile(re.escape(START) + r" sha256=([a-f0-9]{64}) -->(.*?)" + re.escape(END), re.S)
    matches = list(pattern.finditer(original))
    if len(matches) != 1 or original.count(START) != 1 or original.count(END) != 1:
        raise TransportError("README statistics markers changed; publication skipped")
    match = matches[0]
    if hashlib.sha256(match.group(2).encode()).hexdigest() != match.group(1):
        raise TransportError("README generated region was edited; publication skipped")
    return original[:match.start()] + region + original[match.end():]


def publish(git, branch, snapshot, retries=3):
    for _ in range(retries):
        revision = git.fetch(branch)
        if revision is None:
            raise TransportError("knowledge branch missing; README publication skipped")
        # Inventory must belong to the same remote revision we are about to update.
        inv = inventory(git, revision)
        snapshot = {**snapshot, "knowledge_total": inv["total"], "knowledge": inv["states"], "inventory_revision": revision}
        original = git.read(revision, "README.md") or ""
        updated = replace_region(original, markdown(snapshot))
        if updated == original:
            return revision
        commit = git.commit(revision, {"README.md": updated})
        try:
            git.push(branch, commit)
        except TransportError:
            continue
        confirmed = git.fetch(branch)
        if git.read(confirmed, "README.md") == updated:
            return confirmed
    raise TransportError("README retry limit reached; publication pending")
