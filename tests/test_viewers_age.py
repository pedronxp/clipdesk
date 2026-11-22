import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def test_age_s_zero_when_fresh(monkeypatch):
    monkeypatch.setattr(server, "VIEWERS", {})
    monkeypatch.setattr(server, "fetch_viewers", lambda url: 1234)
    snap = server.viewers_snapshot(["https://twitch.tv/a"])
    assert snap["https://twitch.tv/a"]["viewers"] == 1234
    assert snap["https://twitch.tv/a"]["age_s"] == 0
    assert "_ts" not in snap["https://twitch.tv/a"]


def test_age_s_carries_cached_age(monkeypatch):
    """A URL inside the TTL keeps its cached value; age_s tells the client how stale it is."""
    url = "https://twitch.tv/b"
    monkeypatch.setattr(server, "VIEWER_TTL", 600)  # entry at 300 s is inside the TTL
    monkeypatch.setattr(server, "VIEWERS",
                        {url: {"viewers": 500, "platform": "Twitch", "_ts": time.time() - 300,
                               "checked": "12:00:00 UTC"}})
    monkeypatch.setattr(server, "fetch_viewers",
                        lambda u: (_ for _ in ()).throw(AssertionError("should not refetch")))
    snap = server.viewers_snapshot([url])
    assert snap[url]["viewers"] == 500
    assert 290 <= snap[url]["age_s"] <= 300
