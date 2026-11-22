import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "SERVER_LOCK", tmp_path / "state" / "server.lock")
    return tmp_path / "state"


def test_lock_blocks_fresh_alive_server(tmp_path, monkeypatch):
    """A second dashboard would race the same queue/watchlist/settings files (review Important 3)."""
    state = _redirect(tmp_path, monkeypatch)
    state.mkdir(parents=True)
    server.SERVER_LOCK.write_text(json.dumps({"pid": 123, "ts": time.time()}), encoding="utf-8")
    monkeypatch.setattr(server, "pid_alive", lambda pid: pid == 123)
    assert server.acquire_server_lock() is False


def test_lock_takeover_on_dead_pid_or_stale(tmp_path, monkeypatch):
    state = _redirect(tmp_path, monkeypatch)
    state.mkdir(parents=True)
    server.SERVER_LOCK.write_text(json.dumps({"pid": 999, "ts": time.time() - 3600}), encoding="utf-8")
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    assert server.acquire_server_lock() is True
    data = json.loads(server.SERVER_LOCK.read_text(encoding="utf-8"))
    assert data["pid"] != 999  # lock taken over with our own pid


def test_lock_missing_or_garbled_file_allows_start(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)  # even with no file at all
    assert server.acquire_server_lock() is True
    (server.STATE_DIR / "server.lock").write_text("não é json", encoding="utf-8")
    assert server.acquire_server_lock() is True  # garbled lock is taken over
