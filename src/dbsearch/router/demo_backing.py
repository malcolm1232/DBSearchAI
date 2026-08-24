"""Demo-scope local backing for cloud connector kinds (ADR 0009).

A cloud provider (azure_sql/postgres/...) is given a fixture-aware engine factory ONLY on
the demo compose path. When the store's config carries a `fixture: {files: [...csv]}` block,
the store runs on a local `SqliteEngine` instead of connecting to the cloud, while keeping its
real `kind` so `origins.py` still badges it (e.g. "Azure SQL"). LAW 2: this factory is never
installed on the live/user compose path, so a user-submitted `fixture:` is inert there.
"""
from __future__ import annotations

from pathlib import Path as _Path
from typing import Callable

from dbsearch.router.structured import SqlEnginePort, SqliteEngine


def fixture_or_cloud_factory(
    cloud_factory: Callable[[dict], SqlEnginePort],
) -> Callable[[dict], SqlEnginePort]:
    def factory(config: dict) -> SqlEnginePort:
        fixture = config.get("fixture") or {}
        files = fixture.get("files")
        if files:
            return SqliteEngine.from_csv_files(list(files))
        return cloud_factory(config)

    return factory


_FIXTURE_ROOT = _Path(__file__).resolve().parent / "demo" / "fixtures"


def demo_fixture_path(*parts: str) -> str:
    """Absolute path to a bundled demo fixture, e.g. demo_fixture_path('azure_sql', 'sales.csv')."""
    return str(_FIXTURE_ROOT.joinpath(*parts))
