"""V2 governance package (P5-B).

Submodules:
- lifecycle: expiry evaluation, conflict detection, role approval gate.
- metrics: aggregated governance metrics for the dashboard (quality / freshness /
  permission denials / sync backlog).
- api: facade that collects a governance snapshot from a repository.
"""
from __future__ import annotations

from knowbase.v2.governance import lifecycle
from knowbase.v2.governance import dashboard as dashboard_renderer

__all__ = ["lifecycle", "dashboard_renderer"]