"""SigNoz provisioning assets and helpers for FoldOS."""

from __future__ import annotations

from pathlib import Path


def discover_assets() -> list[Path]:
    """Return packaged dashboard and alert JSON assets in stable path order."""
    root = Path(__file__).parent
    return [
        *sorted(root.glob("dashboards/*.json")),
        *sorted(root.glob("alerts/*.json")),
    ]


__all__ = ["discover_assets"]
