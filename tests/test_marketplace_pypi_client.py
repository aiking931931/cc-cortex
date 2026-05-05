"""Unit tests for ``concinno.marketplace.pypi_client``.

Covers cache hit / miss / TTL expiry / corruption recovery / unreachable
fallback. Network is never touched — every test injects a fake
transport.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from concinno.marketplace.pypi_client import (
    PyPIClient,
    PyPIUnreachableError,
)


def _ok_transport(_url: str) -> dict[str, Any]:
    return {"info": {"version": "1.2.3", "summary": "ok"}}


def _fail_transport(_url: str) -> dict[str, Any]:
    raise PyPIUnreachableError("simulated")


def _build_clock() -> tuple[Callable[[], float], list[float]]:
    state = [1_000_000.0]
    return (lambda: state[0], state)


def test_cache_miss_then_hit(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    clock, _state = _build_clock()
    client = PyPIClient(cache_path=cache, transport=_ok_transport, clock=clock)
    out1 = client.list_concinno_skills_packages()
    assert out1
    assert all(r["version"] == "1.2.3" for r in out1)
    # Second call must hit the cache (transport would still succeed but
    # we don't care — what we want is the file write).
    assert cache.is_file()
    out2 = client.list_concinno_skills_packages()
    assert out2 == out1


def test_cache_ttl_expiry(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    clock, state = _build_clock()
    client = PyPIClient(
        cache_path=cache,
        cache_ttl_sec=10,
        transport=_ok_transport,
        clock=clock,
    )
    client.list_concinno_skills_packages()
    # Advance the clock past TTL.
    state[0] += 100
    calls = []

    def counting_transport(url: str) -> dict[str, Any]:
        calls.append(url)
        return _ok_transport(url)

    client._transport = counting_transport  # type: ignore[assignment]
    client.list_concinno_skills_packages()
    assert calls, "expected re-fetch after TTL expiry"


def test_offline_with_stale_cache_returns_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    clock, state = _build_clock()
    client = PyPIClient(
        cache_path=cache,
        cache_ttl_sec=10,
        transport=_ok_transport,
        clock=clock,
    )
    client.list_concinno_skills_packages()  # warm cache
    state[0] += 100  # past TTL
    client._transport = _fail_transport  # type: ignore[assignment]
    out = client.list_concinno_skills_packages()
    # Stale-cache fallback: live fetch failed but the old cache rescues us.
    assert out


def test_offline_with_no_cache_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    client = PyPIClient(cache_path=cache, transport=_fail_transport)
    with pytest.raises(PyPIUnreachableError):
        client.list_concinno_skills_packages()


def test_corrupt_cache_recovers(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{ this is not json", encoding="utf-8")
    client = PyPIClient(cache_path=cache, transport=_ok_transport)
    out = client.list_concinno_skills_packages()
    assert out
    # Corrupt file must have been overwritten with valid JSON.
    import json
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert "packages" in data


def test_invalidate_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    client = PyPIClient(cache_path=cache, transport=_ok_transport)
    client.list_concinno_skills_packages()
    assert cache.is_file()
    client.invalidate_cache()
    assert not cache.is_file()


def test_cache_age_seconds_zero_when_absent(tmp_path: Path) -> None:
    cache = tmp_path / "absent.json"
    client = PyPIClient(cache_path=cache, transport=_ok_transport)
    assert client.cache_age_seconds() == 0


def test_force_refresh_bypasses_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    calls = []

    def counting(url: str) -> dict[str, Any]:
        calls.append(url)
        return _ok_transport(url)

    client = PyPIClient(cache_path=cache, transport=counting)
    client.list_concinno_skills_packages()
    n1 = len(calls)
    client.list_concinno_skills_packages()  # cache hit
    assert len(calls) == n1
    client.list_concinno_skills_packages(force_refresh=True)
    assert len(calls) > n1
