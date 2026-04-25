"""FastAPI backend for the concinno config GUI.

@module concinno.gui.server
@responsibility Expose a tiny localhost-only REST surface over
    ``FEATURE_META`` (read + patch), ``~/.concinno/*.json`` (read +
    atomic write via ``concinno.feature_config.set_feature``), the
    Claude harness permission files (read-only, surfaced so the
    operator sees the full two-layer gate state), and the ZIQ
    posterior file (read-only).

Design notes:

* All routes live under ``/api/…`` — the single-page frontend is
  served from ``/`` as static files. No templating, no React build —
  Vanilla JS fetches JSON and renders.
* Listens on loopback by default. ``--host 0.0.0.0`` is allowed but
  gated behind an explicit CLI flag; emits a stderr warning and
  refuses when the process detects it is running as a non-local user.
* Writes go through ``concinno.feature_config.set_feature`` so the
  origin sidecar (``~/.concinno/preset_origins.json``) records the
  change as ``("gui", <session>)`` — keeps audit parity with
  ``concinno preset`` CLI writes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError as _err:  # pragma: no cover — optional dep
    raise ImportError(
        "concinno.gui requires FastAPI. Install with "
        "`pip install 'concinno[gui]'`."
    ) from _err

from concinno.gui import auth as _auth

_logger = logging.getLogger(__name__)

# Routes that may be hit without auth (used by client-side health probes
# / liveness pings before the token is read off disk).
_AUTH_BYPASS_PATHS = frozenset({"/api/health"})


STATIC_DIR = Path(__file__).parent / "static"

# ── Effect-scope registry ────────────────────────────────────────
#
# For each FEATURE_META key, annotate how quickly a config change
# propagates so the GUI can warn the operator. Kept here (not in
# FEATURE_META itself) to avoid churning every per-feature entry —
# the GUI registry is the SSOT, with ``immediate`` as the pragmatic
# default (guards / gates / per-tool config are re-read on every
# subprocess spawn).
#
# ``session_restart`` — the value is captured once at SessionStart
# hook time (so rendering, inject templates, or session summary
# snapshots). User must close + reopen Claude Code.
#
# ``process_restart`` — long-lived daemons (scheduler, the GUI itself,
# concinno-daemon) cache the value at boot. User must restart the
# specific daemon.
#
# ``immediate`` — each PreToolUse / subprocess reads config fresh.
# ``update_file`` invalidates the in-process singleton, so the next
# Config.feature() call in any new subprocess picks up the change.

_SESSION_RESTART_FEATURES = frozenset({
    "session_switches",
    "session_summary",
    "prompt_guard",
    "streak_ux",
    "language_enforce",
    "cognitive_anchor",
    "deny_marker",
    "token_display",
})

_PROCESS_RESTART_FEATURES = frozenset({
    # Reserved for features that only live inside long-running daemons.
    # Currently none — GUI itself re-reads per request via get_config().
})


def _effect_scope(feature_name: str) -> str:
    if feature_name in _SESSION_RESTART_FEATURES:
        return "session_restart"
    if feature_name in _PROCESS_RESTART_FEATURES:
        return "process_restart"
    return "immediate"


def _harness_settings_files() -> list[Path]:
    home = Path.home()
    cwd = Path.cwd()
    return [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]


def _read_json_soft(p: Path) -> dict[str, Any] | None:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ziq_posterior_path() -> Path:
    return Path.home() / ".concinno" / "ziq_posterior.json"


def _feature_entry(
    name: str, meta: dict[str, Any], cfg, origin: str = "official",
) -> dict[str, Any]:
    """Build a single feature dict for the ``/api/features`` response.

    Added fields vs pre-2.23 shape:
      * ``example`` — plain-English explanation + scenario for the ? tooltip
      * ``ziq_opt_out`` — True when the operator has flipped ZIQ auto-tune
        off for this feature (takes precedence over ``ziq_autotunable``
        meta flag)
      * ``ziq_effective`` — derived: ``ziq_autotunable and not ziq_opt_out``
      * params[p].manual_pinned — operator has explicitly locked this
        param to the current value (ZIQ skips it)
      * params[p].is_modified — current value differs from default

    The ``origin`` argument added in 2.30.1 distinguishes shipped
    ``FEATURE_META`` entries (``"official"``) from user-registered
    entries in ``~/.concinno/user_features.json`` (``"user"``).
    """
    from concinno.gui.feature_examples import example_for

    entry: dict[str, Any] = {
        "name": name,
        "category": meta.get("category"),
        "description": meta.get("description"),
        "example": example_for(name),
        "ziq_autotunable": meta.get("ziq_autotunable", False),
        "cosmetic": meta.get("cosmetic", False),
        "effect_scope": _effect_scope(name),
        # ``source`` distinguishes library-bundled from user-extension
        # features. Shipped ``FEATURE_META`` entries are "official";
        # user-registered entries from ``~/.concinno/user_features.json``
        # are "user". Set via ``origin`` kwarg (2.30.1+).
        "source": origin,
        # 2.36.0a1 schema additions (all optional, default-empty so
        # un-migrated entries render as "not recommended, severity none,
        # no consequence text" — pre-2.36 GUI behaviour).
        "recommended": bool(meta.get("recommended", False)),
        "severity_if_off": meta.get("severity_if_off", "none"),
        "consequences_if_off": meta.get("consequences_if_off", ""),
        "consequences_if_off_en": meta.get("consequences_if_off_en", ""),
        "params": {},
    }
    try:
        entry["enabled"] = bool(cfg.feature(name, "enabled"))
    except Exception:
        entry["enabled"] = None
    try:
        opt_out = cfg.feature(name, "ziq_opt_out")
    except Exception:
        opt_out = None
    entry["ziq_opt_out"] = bool(opt_out) if opt_out is not None else False
    entry["ziq_effective"] = bool(
        entry["ziq_autotunable"] and not entry["ziq_opt_out"]
    )
    for pname, pmeta in meta.get("params", {}).items():
        try:
            current = cfg.feature(name, pname)
        except Exception:
            current = None
        default = pmeta.get("default")
        # manual_pinned is a sidecar key: features.<name>.<pname>__pinned
        try:
            pinned = cfg.feature(name, f"{pname}__pinned")
        except Exception:
            pinned = None
        # Auto-pin heuristic: if operator set a value != default, treat
        # that as an implicit manual lock unless they explicitly cleared
        # the pin. Mirrors switches.md "user explicit > opt-out > ZIQ".
        is_modified = (
            current is not None and current != default
        )
        manual_pinned = bool(pinned) if pinned is not None else is_modified
        entry["params"][pname] = {
            "type": pmeta.get("type"),
            "default": default,
            "current": current,
            "min": pmeta.get("min"),
            "max": pmeta.get("max"),
            "options": pmeta.get("options"),
            "recommended": pmeta.get("recommended"),
            "risk_low": pmeta.get("risk_low"),
            "risk_high": pmeta.get("risk_high"),
            "risk_off": pmeta.get("risk_off"),
            "is_modified": is_modified,
            "manual_pinned": manual_pinned,
        }
    return entry


def _fresh_config():
    """Invalidate the in-process Config singleton then re-read from disk.

    Ensures the GUI surfaces LLM-side writes to ``~/.concinno/*.json``
    and ``~/.claude/hooks/cc_config.json`` without waiting for a GUI
    process restart.
    """
    from concinno.core.config import get_config, reset_config
    reset_config()
    return get_config()


def _register_feature_routes(app: "FastAPI") -> None:
    @app.get("/api/features")
    def list_features() -> JSONResponse:
        from concinno.feature_config import iter_all_features_with_origin
        from concinno.user_features import collision_warnings
        cfg = _fresh_config()
        return JSONResponse({
            "features": [
                _feature_entry(n, m, cfg, origin=o)
                for n, m, o in iter_all_features_with_origin()
            ],
            "collisions": collision_warnings(),
        })

    @app.get("/api/features/digest")
    def get_digest() -> JSONResponse:
        return JSONResponse(_config_digest())

    @app.get("/api/features/collisions")
    def list_collisions() -> JSONResponse:
        """Return warnings from the last three-way merge (shipped /
        user / plugin) plus any plugin load errors.

        Extended in 2.31.0: ``plugin_load_errors`` surfaces
        entry-points ``ep.load()`` failures (malformed packages,
        import-time crashes, schema rejection) so a user with a
        broken plugin sees it in the GUI rather than silent skip.
        """
        # Trigger a fresh merge so the buffer reflects current files.
        from concinno.feature_config import iter_all_features_with_origin
        from concinno.user_features import collision_warnings
        iter_all_features_with_origin()

        plugin_load_errors: list[dict[str, Any]] = []
        try:
            from concinno.plugins import discover_feature_entrypoints
            for plugin_meta in discover_feature_entrypoints():
                if plugin_meta.errors:
                    plugin_load_errors.append({
                        "package": plugin_meta.package,
                        "entry_point": plugin_meta.entry_point_name,
                        "errors": list(plugin_meta.errors),
                    })
        except Exception:
            pass

        return JSONResponse({
            "collisions": collision_warnings(),
            "plugin_load_errors": plugin_load_errors,
        })

    @app.get("/api/features/{name}")
    def get_feature(name: str) -> JSONResponse:
        from concinno.feature_config import iter_all_features_with_origin

        for row_name, row_meta, row_origin in iter_all_features_with_origin():
            if row_name == name:
                return JSONResponse(
                    _feature_entry(name, row_meta, _fresh_config(), origin=row_origin)
                )
        raise HTTPException(status_code=404, detail=f"unknown feature: {name}")

    @app.post("/api/features/{name}")
    def set_feature_value(name: str, body: dict[str, Any]) -> JSONResponse:
        """Body: ``{"key": "enabled", "value": true, "force": false}``"""
        from concinno.feature_config import set_feature

        key = body.get("key")
        value = body.get("value")
        force = bool(body.get("force", False))
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=400, detail="`key` required (str)")
        warnings = set_feature(name, key, value, force=force, origin=("gui",))
        applied = not (warnings and not force)
        return JSONResponse({
            "applied": applied, "warnings": warnings,
            "feature": name, "key": key, "value": value,
        })


def _register_harness_routes(app: "FastAPI") -> None:
    @app.get("/api/harness/settings")
    def list_harness_settings() -> JSONResponse:
        out: list[dict[str, Any]] = []
        for p in _harness_settings_files():
            data = _read_json_soft(p)
            if data is None:
                out.append({"path": str(p), "present": p.is_file(), "permissions": None})
                continue
            perms = data.get("permissions", {})
            out.append({
                "path": str(p),
                "present": True,
                "permissions": {
                    "allow": perms.get("allow", []),
                    "deny": perms.get("deny", []),
                    "ask": perms.get("ask", []),
                },
            })
        return JSONResponse({"files": out})


def _register_ziq_routes(app: "FastAPI") -> None:
    @app.get("/api/ziq/posterior")
    def get_ziq_posterior() -> JSONResponse:
        p = _ziq_posterior_path()
        if not p.is_file():
            return JSONResponse({"present": False, "posterior": {},
                                 "overrides": []})
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse(
                {"present": True, "posterior": {}, "error": str(e),
                 "overrides": []},
            )
        # Decompose into per-feature override rows for the GUI table.
        overrides: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for feat_name, rec in data.items():
                if isinstance(rec, dict):
                    for key, val in rec.items():
                        overrides.append({
                            "feature": feat_name, "key": key,
                            "ziq_value": val,
                        })
        return JSONResponse({
            "present": True, "posterior": data, "overrides": overrides,
        })


_DIGEST_EXCLUDE_SEGMENTS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
_DIGEST_SKILL_SCALE_THRESHOLD = 200


def _config_digest() -> dict[str, Any]:
    """Return a cheap change signal for frontend polling.

    Hashes every ``~/.concinno/*.json`` file's mtime **plus** every
    discoverable ``.claude/skills/.../SKILL.md`` mtime, so the
    frontend auto-refreshes when a new skill is added, renamed, or
    removed — not just when a config JSON changes. Added in 2.30.0.

    Exclusions: any path whose parts contain ``.git``, ``node_modules``,
    ``__pycache__``, ``.venv`` or ``venv`` — these pollute the hash with
    submodule / env churn.

    Canonicalization: skill roots are resolved to absolute paths and
    deduplicated so ``Path.cwd() == Path.home()`` does not double-count.

    Scale fallback: when > 200 SKILL.md files are discovered, the loop
    falls back to hashing top-level skill *directory* mtimes instead.
    Directory mtime flips when contents change on all major filesystems
    and keeps the per-poll cost bounded.
    """
    import hashlib
    h = hashlib.blake2b(digest_size=8)
    candidates: list[Path] = [_ziq_posterior_path()]
    concinno_dir = Path.home() / ".concinno"
    if concinno_dir.is_dir():
        for p in sorted(concinno_dir.glob("*.json")):
            candidates.append(p)
    cc_hooks = Path.home() / ".claude" / "hooks" / "cc_config.json"
    candidates.append(cc_hooks)

    seen_roots: set[Path] = set()
    skill_files: list[Path] = []
    exceeded = False
    for root in _skills_roots():
        if not root.is_dir():
            continue
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if root_resolved in seen_roots:
            continue
        seen_roots.add(root_resolved)
        for sk in root_resolved.rglob("SKILL.md"):
            if any(seg in _DIGEST_EXCLUDE_SEGMENTS for seg in sk.parts):
                continue
            skill_files.append(sk)
            if len(skill_files) > _DIGEST_SKILL_SCALE_THRESHOLD:
                exceeded = True
                break
        if exceeded:
            break

    if exceeded:
        skill_dirs: set[Path] = set()
        for root in _skills_roots():
            if not root.is_dir():
                continue
            try:
                root_resolved = root.resolve()
            except OSError:
                continue
            for sub in root_resolved.iterdir():
                if sub.is_dir() and sub.name not in _DIGEST_EXCLUDE_SEGMENTS:
                    skill_dirs.add(sub.resolve())
        for d in sorted(skill_dirs):
            try:
                st = d.stat()
                h.update(f"{d}:{st.st_mtime_ns}:dir".encode("utf-8"))
            except OSError:
                continue
    else:
        for sk in sorted(skill_files):
            try:
                st = sk.stat()
                h.update(f"{sk}:{st.st_mtime_ns}:file".encode("utf-8"))
            except OSError:
                continue

    mtime = 0.0
    for p in candidates:
        try:
            st = p.stat()
            h.update(f"{p}:{st.st_mtime_ns}".encode("utf-8"))
            mtime = max(mtime, st.st_mtime)
        except FileNotFoundError:
            h.update(f"{p}:absent".encode("utf-8"))
    return {"digest": h.hexdigest(), "mtime": mtime}


# ── Skills discovery ───────────────────────────────────────
#
# User directive 2026-04-24: the Skills registry (``.claude/skills/``)
# is distinct from FEATURE_META and needs its own tab. Skills are
# markdown SOPs + MCP integrations loaded by Claude Code at session
# start. We surface them here as a browsable catalogue; a per-skill
# on/off state is persisted to ``~/.concinno/skills.json``. The CC
# harness itself does not yet read that file (SessionStart hook
# wiring is follow-up), but the file is the SSOT and any future
# enforcement hook can read it directly.

def _skills_roots() -> list[Path]:
    """Directories that may contain Claude Code skill packages."""
    roots = [Path.home() / ".claude" / "skills"]
    cwd_skills = Path.cwd() / ".claude" / "skills"
    if cwd_skills.is_dir():
        roots.append(cwd_skills)
    return roots


def _skills_state_path() -> Path:
    return Path.home() / ".concinno" / "skills.json"


def _read_skills_state() -> dict[str, dict[str, Any]]:
    p = _skills_state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_skills_state(state: dict[str, dict[str, Any]]) -> None:
    p = _skills_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


def _parse_skill_md(path: Path) -> dict[str, Any]:
    """Extract SKILL.md frontmatter.

    Thin wrapper kept for backward compatibility -- real parser moved
    to :mod:`concinno.skill_parser` in 2.31.0 (hardened for
    plugin-supplied frontmatter: BOM / CRLF / block lists / truthy
    tokens / malformed recovery).
    """
    from concinno.skill_parser import parse_skill_md
    return parse_skill_md(path)


def _discover_skills() -> list[dict[str, Any]]:
    """Walk every skills root and return catalogue rows merged with
    on/off state.

    Scope values (2.26.0+):
      * ``user``    — ``~/.claude/skills/<name>/``
      * ``public``  — ``~/.claude/skills/public/<name>/`` (intended
        for packaged distribution alongside Concinno)
      * ``private`` — ``~/.claude/skills/private/<name>/`` (machine-
        only, never shipped)
      * ``project`` — ``<cwd>/.claude/skills/<name>/``

    Dedupe by name; a project-local skill wins over user-level.
    """
    state = _read_skills_state()
    seen: dict[str, dict[str, Any]] = {}
    home_root = Path.home() / ".claude" / "skills"
    cwd_root = Path.cwd() / ".claude" / "skills"
    # User-level scans
    # "official" subdir is the 2.28+ canonical name; "public" is kept
    # as a backward-compatible alias (user directive: "公開" → "官方"
    # because you are the author shipping to others, not merely
    # publishing).
    if home_root.is_dir():
        for child in sorted(home_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name in ("official", "public"):
                _scan_subdir(child, "official", state, seen)
            elif child.name == "private":
                _scan_subdir(child, "private", state, seen)
            else:
                _ingest_skill(child, state, seen, scope="user")
    # Project-level scan (project wins on name collision)
    if cwd_root.is_dir() and cwd_root != home_root:
        for child in sorted(cwd_root.iterdir()):
            if child.is_dir():
                _ingest_skill(child, state, seen, scope="project")
    # Plugin-level scan (2.31.0) -- lowest precedence. ``_ingest_skill``
    # is overwrite-semantics so we must explicitly skip name-collisions
    # before delegating, otherwise plugin would shadow user / project.
    try:
        from concinno.plugins import iter_plugin_skill_roots

        for plugin_root, pkg_name in iter_plugin_skill_roots():
            if plugin_root.is_dir():
                _scan_plugin_root(plugin_root, pkg_name, state, seen)
    except Exception:
        # Plugin discovery failure must not abort skill enumeration.
        pass
    return sorted(seen.values(), key=lambda r: r["name"].lower())


def _scan_subdir(
    parent: Path,
    scope: str,
    state: dict[str, dict[str, Any]],
    into: dict[str, dict[str, Any]],
) -> None:
    for child in sorted(parent.iterdir()):
        if child.is_dir():
            _ingest_skill(child, state, into, scope=scope)


def _scan_plugin_root(
    plugin_root: Path,
    pkg_name: str,
    state: dict[str, dict[str, Any]],
    into: dict[str, dict[str, Any]],
) -> None:
    """Walk one plugin's skills root, skipping name collisions.

    ``_ingest_skill`` overwrites ``into[name]`` so plugin-sourced
    skills must be filtered here (not after the fact) to preserve
    higher-precedence user / project sources.
    """
    scope = f"plugin:{pkg_name}"
    for child in sorted(plugin_root.iterdir()):
        if not child.is_dir():
            continue
        skill_name = _resolve_plugin_skill_name(child)
        if skill_name in into:
            continue
        _ingest_skill(child, state, into, scope=scope)


def _resolve_plugin_skill_name(child: Path) -> str:
    """Best-effort skill-name resolution for a plugin skill dir.

    Honours the ``name:`` field in SKILL.md frontmatter when present
    (a plugin may rename a skill dir-wise vs catalogue-wise). Falls
    back to the directory name when SKILL.md is missing or unreadable.
    """
    default = child.name
    skill_md = child / "SKILL.md"
    if not skill_md.is_file():
        return default
    try:
        meta = _parse_skill_md(skill_md)
    except Exception:
        return default
    return meta.get("name") or default


def _ingest_skill(
    path: Path,
    state: dict[str, dict[str, Any]],
    into: dict[str, dict[str, Any]],
    scope: str,
) -> None:
    from concinno.gui.skill_descriptions import describe as _fallback_desc

    skill_md = path / "SKILL.md"
    meta = _parse_skill_md(skill_md) if skill_md.is_file() else {}
    name = meta.get("name") or path.name
    description = meta.get("description") or ""
    # Fallback: bare-dir skills (no SKILL.md) get a curated blurb
    # written by hand against the actual Concinno source they
    # correspond to (see concinno.gui.skill_descriptions).
    fb_desc, fb_example = _fallback_desc(name)
    if not description and fb_desc:
        description = fb_desc
    model_hint = meta.get("model") or ""
    st = state.get(name, {})
    into[name] = {
        "name": name,
        "dir": str(path),
        "scope": scope,
        "description": description,
        "example": fb_example or description,
        "model_hint": model_hint,
        "enabled": bool(st.get("enabled", True)),
        "has_skill_md": skill_md.is_file(),
    }


def _register_commands_routes(app: "FastAPI") -> None:
    @app.get("/api/commands")
    def list_commands() -> JSONResponse:
        from concinno.commands_sync import list_installed_commands
        return JSONResponse({"commands": list_installed_commands()})

    @app.post("/api/commands/sync")
    def sync_commands() -> JSONResponse:
        from concinno.commands_sync import sync_commands as _sync
        return JSONResponse(_sync())


def _skill_scope_roots() -> dict[str, str]:
    """Return the actual on-disk root for each scope — surfaced once in
    the GUI header so individual skill cards don't repeat the prefix.

    ``official`` is the 2.28+ canonical folder. ``public`` is still
    honoured at scan time as a back-compat alias; if it exists we show
    both paths so the operator sees where legacy content still lives.
    """
    home = Path.home() / ".claude" / "skills"
    cwd = Path.cwd() / ".claude" / "skills"
    official = home / "official"
    legacy_public = home / "public"
    official_label = str(official)
    if legacy_public.is_dir() and not official.is_dir():
        official_label = f"{official} (currently at {legacy_public} — legacy alias)"
    elif legacy_public.is_dir():
        official_label = f"{official}  (+legacy: {legacy_public})"
    return {
        "user": str(home),
        "official": official_label,
        "private": str(home / "private"),
        "project": str(cwd) if cwd.is_dir() else "(no ./.claude/skills/ here)",
    }


def _register_skills_routes(app: "FastAPI") -> None:
    @app.get("/api/skills")
    def list_skills() -> JSONResponse:
        return JSONResponse({
            "skills": _discover_skills(),
            "roots": _skill_scope_roots(),
        })

    @app.post("/api/skills/{name}")
    def set_skill_state(name: str, body: dict[str, Any]) -> JSONResponse:
        if "enabled" not in body:
            raise HTTPException(
                status_code=400, detail="body requires `enabled` (bool)",
            )
        enabled = bool(body["enabled"])
        state = _read_skills_state()
        state.setdefault(name, {})["enabled"] = enabled
        _write_skills_state(state)
        return JSONResponse({"name": name, "enabled": enabled})


def _register_state_routes(app: "FastAPI") -> None:
    @app.get("/api/concinno/state")
    def get_concinno_state() -> JSONResponse:
        from concinno.release_authorization import describe_current_config

        state: dict[str, Any] = {"release_authorization": describe_current_config()}
        try:
            from concinno.core.config import get_config
            c = get_config()
            state["toast_enabled"] = c.toast_enabled
            state["toast_app_id"] = c.toast_app_id
        except Exception:
            pass
        try:
            from concinno.core.notify import _get_locale
            state["locale"] = _get_locale()
        except Exception:
            pass
        try:
            from concinno.handoff_engine import get_handoff_mode
            state["handoff_mode"] = get_handoff_mode()
        except Exception:
            pass
        return JSONResponse(state)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Reject any request whose ``Authorization: Bearer …`` header does
    not match the in-process expected token.

    Bypass paths (kept narrow) live in :data:`_AUTH_BYPASS_PATHS` -- only
    ``/api/health`` is exempt so loopback liveness probes can run before
    a client has read the token from disk.

    Token comparison uses :func:`hmac.compare_digest` via
    :func:`concinno.gui.auth.verify_token_header`.
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


def _register_health_route(app: "FastAPI") -> None:
    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})


def create_app(*, token: str | None = None,
               token_path: Path | None = None) -> "FastAPI":
    """Build the FastAPI app and persist a per-process bearer token.

    Args:
        token: Override the auto-generated token (mostly for tests).
            When omitted, a fresh :func:`auth.generate_token` is created.
        token_path: Override the token file location (mostly for tests).
            When omitted, :func:`auth.get_token_path` is used.

    The token is written atomically to disk before the app object is
    returned so any client (VS Code extension, federated switcher) can
    read it the moment ``concinno gui`` reports the bind address.

    Raises:
        TokenAuthError: When the token file cannot be written
            (fail-fast — better to refuse to start than to silently
            run unauthenticated).
    """
    chosen_token = token or _auth.generate_token()
    written_path = _auth.write_token(chosen_token, path=token_path)
    _logger.info(
        "concinno.gui: token written to %s; clients must send "
        "Authorization: Bearer <token>",
        written_path,
    )

    app = FastAPI(
        title="Concinno Config GUI",
        description=(
            "Visual control over FEATURE_META switches + user config files. "
            "Pair with ~/.claude/rules/switches.md two-layer gate SOP."
        ),
        version="0.1.0",
    )
    app.add_middleware(BearerTokenMiddleware, expected_token=chosen_token)
    _register_health_route(app)
    _register_feature_routes(app)
    _register_harness_routes(app)
    _register_ziq_routes(app)
    _register_skills_routes(app)
    _register_commands_routes(app)
    _register_state_routes(app)
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    # Stash for test introspection only — not part of the public API.
    app.state.gui_token = chosen_token
    app.state.gui_token_path = written_path
    return app


def run(host: str = "127.0.0.1", port: int = 8400, reload: bool = False) -> None:
    """Block on uvicorn until SIGINT."""
    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover
        raise ImportError(
            "concinno.gui needs uvicorn. Install with "
            "`pip install 'concinno[gui]'`."
        ) from err

    # Loopback default; refuse 0.0.0.0 unless explicit operator consent
    if host not in ("127.0.0.1", "localhost") and not os.environ.get(
        "CONCINNO_GUI_ALLOW_PUBLIC_BIND"
    ):
        raise SystemExit(
            f"concinno gui refuses host={host!r}; set env "
            "CONCINNO_GUI_ALLOW_PUBLIC_BIND=1 to override (loopback-only "
            "by default — this UI exposes config-mutation endpoints)."
        )
    uvicorn.run(
        "concinno.gui.server:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )
