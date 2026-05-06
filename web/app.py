"""Starlette app: landing page at /, Datasette mounted at /db/.

Single ASGI app, single uvicorn process. Datasette is configured with
base_url='/db/' so its templates render correct sub-path URLs once
Starlette's Mount has stripped the prefix.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from datasette.app import Datasette
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .data import db_path
from .pages import index_route, search_redirect

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_METADATA = ROOT / "metadata.yml"


def _load_metadata(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def create_app(
    *,
    matter_db: Path | str | None = None,
    metadata_path: Path | str | None = None,
) -> Starlette:
    matter_db_p = Path(matter_db) if matter_db else db_path()
    metadata_p = Path(metadata_path) if metadata_path else DEFAULT_METADATA
    metadata = _load_metadata(metadata_p)

    if not matter_db_p.exists():
        raise FileNotFoundError(f"matter.db not found at {matter_db_p}")

    datasette = Datasette(
        files=[str(matter_db_p)],
        metadata=metadata,
        immutables=[str(matter_db_p)],   # read-only mount
        settings={
            "base_url": "/db/",
            "default_page_size": 50,
            "max_returned_rows": 5000,
            "suggest_facets": False,
        },
    )
    ds_asgi = datasette.app()

    routes = [
        Route("/", index_route),
        Route("/search", search_redirect),
        Mount(
            "/static",
            app=StaticFiles(directory=str(ROOT / "static")),
            name="static",
        ),
        Mount("/db", app=ds_asgi),
    ]

    app = Starlette(routes=routes)
    app.state.page_cache = {}
    app.state.db_path = matter_db_p
    return app


# Default app for `uvicorn web.app:app` (production / Docker).
# Built lazily on first attribute access so simply importing this module
# from a test never touches data/matter.db. Production uvicorn imports
# `app` and triggers the build; if the DB is missing it fails fast with
# a clear FileNotFoundError.
_default_app: Starlette | None = None


def __getattr__(name: str) -> Starlette:
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(name)
