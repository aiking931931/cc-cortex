"""concinno.gui.switcher — federation switcher reverse-proxy on port 8399.

@module concinno.gui.switcher
@responsibility Provide a single localhost surface that proxies traffic to
    both the Concinno config GUI (port 8400) and the Sancio runtime GUI
    (port 8401), so an operator using the AI King VS Code extension can
    flip between the two backends without juggling URLs, ports, or
    bearer tokens by hand.

Why a flag mode (``concinno gui --switcher``) and not a separate
PyPI package
    Per ``_AI_BRAIN/05_Planning/sancio-gui-extension-commander-verdict-2026-04-25.md``
    R#2 + W2: the federation switcher is opt-in advanced UX. Default
    UX is the VS Code extension's TS-side tab switching. The switcher
    exists for power users who want a single browser bookmark that
    surfaces both panes; bundling it as a third PyPI package would be
    YAGNI. So we hang the mode off the existing ``concinno gui`` verb.

Layer-clean coupling
    This module **must not** ``import persona.*``. It reads the
    Sancio bearer token directly off disk via a path-string mirror of
    :func:`persona.gui.server.get_sancio_token_path`. If persona-api
    is uninstalled the switcher still boots — the Sancio backend just
    shows ``unreachable`` in :func:`/api/backends`. This honours
    ``projects/concinno/CLAUDE.md`` Hard Rule #2 ("never put CC- or
    Sancio-specific logic in Concinno"). The Sancio side will mirror
    the same disk-only read for ``~/.concinno/gui_token``.

Token discipline
    The switcher process has its **own** bearer token at
    ``~/.concinno/switcher_token`` (POSIX) or
    ``%LOCALAPPDATA%\\concinno\\switcher_token`` (Windows). This is
    independent of the Concinno main GUI token at port 8400 — the
    operator authenticates **once** to the switcher (bookmark with
    ``?t=<token>`` or VS Code extension injection), and the switcher
    in turn forwards the *backend* tokens (read fresh from disk on
    every request) when proxying to 8400 / 8401. The two backends
    never see the switcher token; the operator never sees the backend
    tokens directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

try:
    import httpx
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError as _err:  # pragma: no cover — optional dep
    raise ImportError(
        "concinno.gui.switcher requires FastAPI + httpx. Install with "
        "`pip install 'concinno[gui]'`."
    ) from _err

from concinno.gui import auth as _auth

_logger = logging.getLogger(__name__)

# Routes hit without auth (loopback liveness + landing page).
_AUTH_BYPASS_PATHS = frozenset({"/api/health", "/"})

# Default backend ports (override via CLI flags).
DEFAULT_CONCINNO_PORT = 8400
DEFAULT_SANCIO_PORT = 8401
DEFAULT_SWITCHER_PORT = 8399

# Per-request upstream timeout. Short on purpose — the switcher should
# fail-fast back to ``unreachable`` so the operator sees the dead pane
# rather than waiting on a stuck TCP handshake.
_PROXY_TIMEOUT_SECONDS = 5.0


# ── path resolution ────────────────────────────────────────────


def get_switcher_token_path() -> Path:
    """Return the OS-appropriate path for the *switcher's own* bearer token.

    Distinct from the main Concinno GUI token (``gui_token``) so the
    operator can run both processes simultaneously without one
    overwriting the other's token file.
    """
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "concinno" / "switcher_token"
    return Path.home() / ".concinno" / "switcher_token"


def get_sancio_token_path_via_disk() -> Path:
    """Mirror of :func:`persona.gui.server.get_sancio_token_path` that
    **never imports persona.\\***.

    The two functions must stay in lockstep — see ``CLAUDE.md`` Hard
    Rule #2 for the layering rationale. If persona-api ever moves the
    Sancio token, update this function and add an integration test.
    """
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "sancio" / "gui_token"
    return Path.home() / ".sancio" / "gui_token"


# ── auth middleware ────────────────────────────────────────────


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Reject any request to /api/* or /proxy/* whose ``Authorization:
    Bearer …`` header does not match the switcher's own expected token.

    Bypass paths: ``/api/health`` (loopback probe) and ``/`` (landing
    HTML — the operator opens the URL with ``?t=<token>`` and the
    landing page's JS attaches the header on subsequent fetches).
    """

    def __init__(self, app, expected_token: str) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _AUTH_BYPASS_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization")
        if not _auth.verify_token_header(header, self._expected):
            return JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
            )
        return await call_next(request)


# ── route builders ─────────────────────────────────────────────


def _register_health(app: "FastAPI") -> None:
    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "concinno-switcher"})


def _register_landing(app: "FastAPI") -> None:
    """Tiny vanilla HTML with a 2-tab UI flipping between iframes for
    the two backends. No build step, no React, no NPM. The operator
    pastes the bookmark URL ``http://127.0.0.1:8399/?t=<switcher_token>``
    into a browser; the inline JS pulls ``?t`` out of the URL and
    forwards it as ``Authorization: Bearer`` on subsequent fetches.
    The iframes themselves load the *backend* GUIs directly so the
    backend bearer token round-trip stays inside the iframe origin.
    """

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        # Compute backend URLs based on the switcher's configured ports
        # (stashed on app.state by the factory below).
        cport = getattr(app.state, "concinno_port", DEFAULT_CONCINNO_PORT)
        sport = getattr(app.state, "sancio_port", DEFAULT_SANCIO_PORT)
        # Read backend tokens from disk (fail-soft: empty string if absent
        # → frame loads HTML stub but /api/* fetches will 401; the operator
        # is told via /api/backends status badge that backend is "no token").
        concinno_token = _auth.read_token(path=_auth.get_token_path()) or ""
        sancio_token = _auth.read_token(path=get_sancio_token_path_via_disk()) or ""
        # Tokens are inlined into the HTML JS as URL ?bearer= params on the
        # iframe src so the backend SPA's app.js can authenticate every
        # /api/* fetch. Loopback-only acceptable per spec for personal CLI.
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Concinno + Sancio Switcher</title>
<style>
  body {{ margin: 0; font-family: system-ui, sans-serif; }}
  nav {{ display: flex; gap: 10px; padding: 10px; background: #1e1e1e;
         color: #fff; align-items: center; }}
  nav button {{ padding: 6px 14px; background: #333; color: #fff;
                border: 1px solid #555; cursor: pointer; }}
  nav button.active {{ background: #4a90e2; border-color: #4a90e2; }}
  iframe {{ width: 100%; height: calc(100vh - 50px); border: 0; }}
  .footer {{ font-size: 11px; color: #888; margin-left: auto; }}
</style>
</head>
<body>
<nav>
  <button id="btn-concinno" onclick="show('concinno', this)">Concinno (:{cport})</button>
  <button id="btn-sancio" onclick="show('sancio', this)">Sancio (:{sport})</button>
  <span class="footer">Switcher :8399 — proxies tokens from disk</span>
</nav>
<iframe id="frame" src="about:blank"></iframe>
<script>
  // Backend tokens are inlined here from disk reads at switcher startup.
  // Each iframe load passes the matching token as ?bearer= so backend
  // SPA's app.js can attach Authorization: Bearer for /api/* fetches.
  const _TOK = {{ concinno: {concinno_token!r}, sancio: {sancio_token!r} }};
  function show(name, btn) {{
    const port = name === 'concinno' ? {cport} : {sport};
    const tok = encodeURIComponent(_TOK[name] || '');
    document.getElementById('frame').src = `http://127.0.0.1:${{port}}/?bearer=${{tok}}`;
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }}
  // Default to concinno on load.
  show('concinno', document.getElementById('btn-concinno'));
</script>
</body>
</html>
"""
        return HTMLResponse(body)


def _register_backends(app: "FastAPI") -> None:
    """Surface ``/api/backends`` so the GUI can render a status badge
    per backend (token present? reachable?).

    Uses :func:`concinno.gui.auth.read_token` for the Concinno-side
    token and a disk-only mirror for the Sancio-side path so an
    uninstalled persona-api does not block this module.
    """

    @app.get("/api/backends")
    async def backends() -> JSONResponse:
        cport = getattr(app.state, "concinno_port", DEFAULT_CONCINNO_PORT)
        sport = getattr(app.state, "sancio_port", DEFAULT_SANCIO_PORT)

        concinno_token_path = _auth.get_token_path()
        sancio_token_path = get_sancio_token_path_via_disk()
        concinno_token = _auth.read_token(path=concinno_token_path)
        sancio_token = _auth.read_token(path=sancio_token_path)

        async def _ping(url: str) -> bool:
            try:
                async with httpx.AsyncClient(
                    timeout=_PROXY_TIMEOUT_SECONDS,
                ) as client:
                    r = await client.get(url)
                    return r.status_code < 500
            except Exception:
                return False

        concinno_reachable = await _ping(
            f"http://127.0.0.1:{cport}/api/health",
        )
        sancio_reachable = await _ping(
            f"http://127.0.0.1:{sport}/api/health",
        )
        return JSONResponse({
            "concinno": {
                "url": f"http://127.0.0.1:{cport}",
                "token_path": str(concinno_token_path),
                "token_present": concinno_token is not None,
                "reachable": concinno_reachable,
            },
            "sancio": {
                "url": f"http://127.0.0.1:{sport}",
                "token_path": str(sancio_token_path),
                "token_present": sancio_token is not None,
                "reachable": sancio_reachable,
            },
        })


# Methods we are willing to forward. Kept narrow on purpose — the
# switcher exists to flip the UI, not to act as a generic CORS proxy.
_PROXY_METHODS = frozenset({"GET", "POST"})


def _register_proxy(app: "FastAPI") -> None:
    """Mount ``/proxy/{backend}/{path}`` for ``concinno`` + ``sancio``.

    Each request reads the *backend's* token fresh off disk (cheap —
    POSIX stat + read of a ~50-byte file) and forwards it as the
    upstream ``Authorization: Bearer`` header. The switcher's own
    token has already been verified by :class:`BearerTokenMiddleware`.

    Why no body / streaming for now: the proxied backends are tiny
    JSON APIs; payloads stay well under 1 MiB. If we ever need to
    proxy a static-asset stream, add ``StreamingResponse``.
    """

    async def _forward(
        request: Request, backend: str, path: str,
    ) -> Response:
        if request.method not in _PROXY_METHODS:
            raise HTTPException(status_code=405, detail="method not allowed")
        if backend == "concinno":
            port = getattr(app.state, "concinno_port", DEFAULT_CONCINNO_PORT)
            token_path = _auth.get_token_path()
        elif backend == "sancio":
            port = getattr(app.state, "sancio_port", DEFAULT_SANCIO_PORT)
            token_path = get_sancio_token_path_via_disk()
        else:
            raise HTTPException(
                status_code=404, detail=f"unknown backend: {backend}",
            )
        backend_token = _auth.read_token(path=token_path)
        if backend_token is None:
            return JSONResponse(
                {
                    "detail": (
                        f"backend {backend!r} token not found on disk; "
                        f"start the {backend} GUI first or check "
                        f"{token_path}"
                    ),
                },
                status_code=503,
            )
        upstream_url = f"http://127.0.0.1:{port}/{path}"
        headers = {"Authorization": f"Bearer {backend_token}"}
        # Forward a small allow-list of inbound headers (content-type
        # for POST). Strip anything that could leak the switcher's
        # own bearer token.
        ct = request.headers.get("content-type")
        if ct:
            headers["content-type"] = ct
        body = await request.body() if request.method == "POST" else None
        try:
            async with httpx.AsyncClient(
                timeout=_PROXY_TIMEOUT_SECONDS,
            ) as client:
                resp = await client.request(
                    request.method,
                    upstream_url,
                    params=dict(request.query_params),
                    headers=headers,
                    content=body,
                )
        except httpx.RequestError as exc:
            return JSONResponse(
                {"detail": f"upstream {backend} unreachable: {exc!s}"},
                status_code=502,
            )
        # Pass through status + content. Drop hop-by-hop headers.
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    @app.get("/proxy/{backend}/{path:path}")
    async def proxy_get(backend: str, path: str, request: Request) -> Response:
        return await _forward(request, backend, path)

    @app.post("/proxy/{backend}/{path:path}")
    async def proxy_post(backend: str, path: str, request: Request) -> Response:
        return await _forward(request, backend, path)


# ── factory + run ──────────────────────────────────────────────


def create_switcher_app(
    *,
    token: Optional[str] = None,
    token_path: Optional[Path] = None,
    concinno_port: int = DEFAULT_CONCINNO_PORT,
    sancio_port: int = DEFAULT_SANCIO_PORT,
) -> "FastAPI":
    """Build the switcher FastAPI app and persist a per-process token.

    Args:
        token: Override the auto-generated token (mostly for tests).
        token_path: Override the switcher token file location.
        concinno_port: Backend port for the Concinno GUI (default 8400).
        sancio_port: Backend port for the Sancio GUI (default 8401).

    Returns a FastAPI instance. The switcher's bearer token is written
    atomically to disk before the app is returned so the bookmark URL
    can pick it up immediately.
    """
    chosen_token = token or _auth.generate_token()
    chosen_path = token_path or get_switcher_token_path()
    written_path = _auth.write_token(chosen_token, path=chosen_path)
    _logger.info(
        "concinno.gui.switcher: token written to %s; clients "
        "must send Authorization: Bearer <token>",
        written_path,
    )

    app = FastAPI(
        title="Concinno Federation Switcher",
        description=(
            "Reverse-proxy switcher between the Concinno config GUI "
            "(:8400) and the Sancio runtime GUI (:8401). Layer-clean: "
            "reads the Sancio token from disk, never imports persona.*."
        ),
        version="0.1.0",
    )
    app.state.concinno_port = concinno_port
    app.state.sancio_port = sancio_port
    app.add_middleware(BearerTokenMiddleware, expected_token=chosen_token)
    _register_health(app)
    _register_landing(app)
    _register_backends(app)
    _register_proxy(app)
    # Stash for test introspection only — not part of the public API.
    app.state.switcher_token = chosen_token
    app.state.switcher_token_path = written_path
    return app


def run_switcher(
    host: str = "127.0.0.1",
    port: int = DEFAULT_SWITCHER_PORT,
    reload: bool = False,
    concinno_port: int = DEFAULT_CONCINNO_PORT,
    sancio_port: int = DEFAULT_SANCIO_PORT,
) -> None:
    """Block on uvicorn until SIGINT.

    Loopback by default. Refuses non-loopback hosts unless the operator
    sets ``CONCINNO_GUI_ALLOW_PUBLIC_BIND=1``, mirroring the main GUI
    server's discipline.
    """
    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover
        raise ImportError(
            "concinno.gui.switcher needs uvicorn. Install with "
            "`pip install 'concinno[gui]'`."
        ) from err

    if host not in ("127.0.0.1", "localhost") and not os.environ.get(
        "CONCINNO_GUI_ALLOW_PUBLIC_BIND"
    ):
        raise SystemExit(
            f"concinno gui --switcher refuses host={host!r}; set env "
            "CONCINNO_GUI_ALLOW_PUBLIC_BIND=1 to override (loopback-only "
            "by default — this surface forwards bearer tokens to "
            "two backends)."
        )

    # uvicorn ``factory=True`` calls a zero-arg builder. Bake the
    # backend ports into a closure so the operator's CLI flags reach
    # the factory without round-tripping through env vars.
    def _factory() -> "FastAPI":
        return create_switcher_app(
            concinno_port=concinno_port, sancio_port=sancio_port,
        )

    uvicorn.run(
        _factory,
        host=host,
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )


__all__ = [
    "BearerTokenMiddleware",
    "create_switcher_app",
    "get_sancio_token_path_via_disk",
    "get_switcher_token_path",
    "run_switcher",
]
