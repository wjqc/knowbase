"""V2 governance dashboard HTML renderer (P5-B3).

Renders a `GovernanceSnapshot` to a single-file HTML page with no external
resources. Mirrors the visual language of V1 ``knowbase.dashboard`` (single
inline stylesheet, ASCII escapes for user data) while exposing the four new
V2 metric categories: quality / freshness / permissions / sync backlog.

Why a renderer (not a server):
- Snapshot is already an in-memory dataclass; HTML is pure transformation.
- Keeps ``api.py`` decoupled from any web framework and unit-testable.
- HTML is escaped defensively in case metrics fields ever carry user text.

Usage:
    from knowbase.v2.repositories.sqlite_repo import V2Repository
    from knowbase.v2.governance.dashboard import write_dashboard

    with V2Repository(repo_path) as repo:
        snapshot = collect_governance_snapshot(repo)
        write_dashboard(snapshot, Path("governance.html"))
"""
from __future__ import annotations

import html
from pathlib import Path

from knowbase.v2.governance.api import GovernanceSnapshot


def _esc(value) -> str:
    """Escape arbitrary value for safe HTML embedding (text context)."""
    if value is None:
        return "—"
    return html.escape(str(value), quote=False)


def _metric_card(title: str, value, subtitle: str = "") -> str:
    return (
        f'<div class="metric"><b>{_esc(value)}</b>'
        f'<span class="title">{_esc(title)}</span>'
        f'{"<small>" + _esc(subtitle) + "</small>" if subtitle else ""}'
        f'</div>'
    )


def _quality_section(q) -> str:
    parts = [
        _metric_card("知识总量", q.total_documents),
        _metric_card("疑似问题", sum(q.flagged.values()),
                     "contradicted / tombstoned / error / superseded"),
        _metric_card("低置信度", q.low_confidence_count,
                     "confidence 为 once / 空 / 缺失"),
    ]
    # freshness buckets as compact sub-row
    if q.freshness_buckets:
        buckets = " · ".join(f"{k} {v}" for k, v in sorted(q.freshness_buckets.items()))
        parts.append(f'<div class="metric metric-wide"><b>{_esc(buckets)}</b>'
                     f'<span class="title">新鲜度分桶</span>'
                     f'<small>fresh / aging / stale / unknown</small></div>')
    return '<section><h2>质量</h2><div class="cards">' + "".join(parts) + "</div></section>"


def _freshness_section(f) -> str:
    median = "—" if f.median_days_remaining is None else f"{f.median_days_remaining:.0f}"
    soonest = "—" if f.soonest_expiry_days is None else f"{f.soonest_expiry_days:.0f}"
    parts = [
        _metric_card("过期", len(f.expired_doc_ids), "valid_until 已过期"),
        _metric_card("即将过期", len(f.expiring_doc_ids), "warning_days 区间内"),
        _metric_card("剩余中位数(天)", median),
        _metric_card("最近过期(天)", soonest),
    ]
    return ('<section><h2>新鲜度 / 有效期</h2>'
            '<div class="cards">' + "".join(parts) + "</div></section>")


def _permissions_section(p) -> str:
    parts = [
        _metric_card("拒绝总数", p.denied_count, "失败操作中匹配权限错误 token"),
        _metric_card("未分类失败", p.unknown_count, "失败但 error 不含权限关键词"),
        _metric_card("总操作数", p.total_operations),
    ]
    if p.denied_by_kind:
        rows = " · ".join(f"{_esc(k)} {_esc(v)}" for k, v in sorted(p.denied_by_kind.items()))
        parts.append(f'<div class="metric metric-wide"><b>{rows}</b>'
                     f'<span class="title">按操作类型分布</span></div>')
    return '<section><h2>权限拒绝</h2><div class="cards">' + "".join(parts) + "</div></section>"


def _sync_section(s) -> str:
    parts = [
        _metric_card("源总数", s.total_sources),
        _metric_card("过期源", s.stale_sources,
                     f"SLA {s.stale_sources and '已超阈值' or '0 — 健康'}"),
        _metric_card("待处理 ops", s.total_pending_ops),
        _metric_card("冲突文档", s.total_conflicted_docs),
    ]
    if s.last_failed_sources:
        failed = " · ".join(_esc(x) for x in s.last_failed_sources[:10])
        parts.append(f'<div class="metric metric-wide"><b>{failed}</b>'
                     f'<span class="title">最近失败的源</span></div>')
    return '<section><h2>同步积压</h2><div class="cards">' + "".join(parts) + "</div></section>"


_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<title>Knowbase V2 · 治理看板</title>
<style>
:root{--bg:#f2f0e9;--ink:#193831;--muted:#596c65;--line:#cbd3ca;--accent:#b44c2c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif}
main{max-width:1280px;margin:auto;padding:32px}header{border-top:5px solid var(--ink);padding:16px 0 24px}
h1{font:36px/1.2 Georgia,serif;margin:8px 0}h2{font-size:20px;margin:32px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
.cards{display:flex;flex-wrap:wrap;border-block:1px solid var(--line);margin-bottom:8px}
.metric{flex:1 1 180px;padding:18px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.metric b{display:block;font:32px Georgia,serif;margin-bottom:4px}
.metric .title{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.metric small{display:block;color:var(--muted);margin-top:4px;font-size:11px}
.metric-wide{flex-basis:100%}
small.ts{color:var(--muted)}p.lede{color:var(--muted);max-width:720px}
</style>
<main>
<header><small>KNOWBASE V2 / GOVERNANCE</small>
<h1>治理看板</h1>
<p class="lede">质量 · 新鲜度 · 权限拒绝 · 同步积压 · 基于 V2 仓储的离线快照</p>
<small class="ts">生成于 __GENERATED_AT__</small></header>
__SECTIONS__
</main>
"""


def render_dashboard_html(snapshot: GovernanceSnapshot) -> str:
    """Render a `GovernanceSnapshot` to a single-file HTML string.

    All user-controlled fields are HTML-escaped; the page has no external
    resources (fonts / scripts / images) and renders correctly offline.
    """
    sections = "".join([
        _quality_section(snapshot.quality),
        _freshness_section(snapshot.freshness),
        _permissions_section(snapshot.permissions),
        _sync_section(snapshot.sync_backlog),
    ])
    return _TEMPLATE.replace("__GENERATED_AT__", _esc(snapshot.generated_at)) \
                    .replace("__SECTIONS__", sections)


def write_dashboard(snapshot: GovernanceSnapshot, output: Path) -> Path:
    """Render and persist dashboard HTML; creates parent dirs as needed."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard_html(snapshot), encoding="utf-8")
    return output


__all__ = ["render_dashboard_html", "write_dashboard"]
