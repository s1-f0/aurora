"""Pins for the connector's per-process client cache.

Before: every connect_to_redis_with_fail_fast() call built a fresh redis.Redis (its own pool)
after a raw-socket probe. Three OpenCode hook workers alone held 72 of the embedded server's 105
sockets, and command latency sat at 250 ms. A client is thread-safe and its pool reconnects after
disconnect(), so one client per (host, port, db, decode, timeout) is correct.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import redis as redis_mod  # noqa: E402

from core.foundation import redis_connection as rc  # noqa: E402


class _FakeClient:
    made: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        _FakeClient.made.append(self)

    def ping(self):
        return True

    def close(self):
        self.closed = True


def _patch(monkeypatch):
    _FakeClient.made = []
    monkeypatch.setattr(rc, "REDIS_LIBRARY_AVAILABLE", True)
    monkeypatch.setattr(rc, "probe_redis_reachable", lambda *a, **k: True)
    monkeypatch.setattr(rc, "_note_reachable", lambda *a, **k: None)
    monkeypatch.setattr(redis_mod, "Redis", _FakeClient)
    rc.reset_clients()


def test_one_client_per_shape(monkeypatch):
    _patch(monkeypatch)
    a = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    b = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    assert a is b
    assert len(_FakeClient.made) == 1, "the second call must not build a new pool"

    other_db = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379, db=2)
    assert other_db is not a
    assert len(_FakeClient.made) == 2


def test_cached_calls_skip_the_probe(monkeypatch):
    _patch(monkeypatch)
    probes = []
    monkeypatch.setattr(rc, "probe_redis_reachable", lambda *a, **k: probes.append(1) or True)
    rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    assert len(probes) == 1, "a cached client must not pay a reachability probe per call"


def test_reset_drops_the_cache(monkeypatch):
    _patch(monkeypatch)
    a = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    rc.reset_clients()
    assert a.closed
    b = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    assert b is not a
    assert len(_FakeClient.made) == 2


def test_pool_is_bounded(monkeypatch):
    _patch(monkeypatch)
    client = rc.connect_to_redis_with_fail_fast(host="127.0.0.1", port=16379)
    assert client.kwargs["max_connections"] == 16
