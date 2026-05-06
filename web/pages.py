"""Landing-page handlers for the Starlette app."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from . import data

ROOT = Path(__file__).resolve().parent

env = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render_index() -> str:
    stats = data.get_stats()
    top_vendors = data.get_top_vendors(10)
    new_this_week = data.get_new_this_week(15)
    return env.get_template("index.html").render(
        stats=stats,
        top_vendors=top_vendors,
        new_this_week=new_this_week,
    )


async def index_route(request: Request) -> HTMLResponse:
    cache = request.app.state.page_cache
    html = data.cached(cache, "index", _render_index)
    return HTMLResponse(html)


async def search_redirect(request: Request) -> RedirectResponse:
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return RedirectResponse("/", status_code=303)
    qs = urlencode({"q": q})
    return RedirectResponse(f"/db/matter/search_products?{qs}", status_code=303)
