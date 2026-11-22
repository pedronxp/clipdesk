import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server

NOW = 1_800_000_000.0  # fixed "now" for every test


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _running(**over):
    m = {"status": "running", "last_activity": NOW - 20, "chat_last_ts": NOW - 30,
         "started": _iso(NOW - 600), "spike_rows_session": [["2026-11-20 14:00:00", "12:05", "92", "gta", "spike_1.mp4"]],
         "pipeline": None, "monitor_meta": None, "log_error": False, "log": [],
         "void": False, "ended_at": None}
    m.update(over)
    return m


def test_gravando_healthy():
    cs = server.card_state(_running(), now=NOW)
    assert cs["state"] == "gravando" and cs["anomalies"] == []


def test_suspeito_thresholds():
    """89s without a block is fine; 91s flags 'sem bloco novo' (Review Focus: thresholds)."""
    assert server.card_state(_running(last_activity=NOW - 89), now=NOW)["state"] == "gravando"
    cs = server.card_state(_running(last_activity=NOW - 91), now=NOW)
    assert cs["state"] == "suspeito" and any("sem bloco novo" in a for a in cs["anomalies"])


def test_suspeito_chat_dead():
    cs = server.card_state(_running(chat_last_ts=NOW - 181), now=NOW)
    assert cs["state"] == "suspeito" and any("chat" in a for a in cs["anomalies"])


def test_suspeito_no_spike_25min():
    cs = server.card_state(_running(started=_iso(NOW - 26 * 60), spike_rows_session=[]), now=NOW)
    assert cs["state"] == "suspeito" and any("sem pico" in a for a in cs["anomalies"])


def test_processando():
    cs = server.card_state(_running(status="done", pipeline={"state": "queued", "position": 1}), now=NOW)
    assert cs["state"] == "processando"


def test_erro_from_monitor_meta():
    cs = server.card_state(_running(status="done", monitor_meta={"error": "moments.py falhou"}), now=NOW)
    assert cs["state"] == "erro" and cs["reason"] == "moments.py falhou"


def test_erro_from_log():
    m = _running(status="stopped", log_error=True,
                 log=["recording live", "Traceback (most recent call last):", "  File x", "ValueError: bad"])
    cs = server.card_state(m, now=NOW)
    assert cs["state"] == "erro" and "ValueError: bad" in (cs["reason"] or "")


def test_sem_nada_countdown():
    m = _running(status="done", void=True, ended_at=NOW - 5 * 60, pipeline=None, monitor_meta=None)
    cs = server.card_state(m, now=NOW)
    assert cs["state"] == "sem-nada" and cs["countdown_min"] == 10  # 15 grace - 5 elapsed


def test_countdown_floor():
    m = _running(status="done", void=True, ended_at=NOW - 40 * 60, pipeline=None, monitor_meta=None)
    assert server.card_state(m, now=NOW)["countdown_min"] == 0


def test_pronto():
    cs = server.card_state(_running(status="done", monitor_meta={"clips": [{"n": 1}]}), now=NOW)
    assert cs["state"] == "pronto"


def test_offline():
    cs = server.card_state(_running(status="stopped"), now=NOW)
    assert cs["state"] == "offline"


def test_precedence_error_over_void():
    """erro wins over sem-nada, but the countdown still travels on the card (spec D1)."""
    m = _running(status="done", void=True, ended_at=NOW - 300, monitor_meta={"error": "boom"}, pipeline=None)
    cs = server.card_state(m, now=NOW)
    assert cs["state"] == "erro" and cs["reason"] == "boom" and cs["countdown_min"] == 10


def test_processando_beats_pronto():
    cs = server.card_state({"status": "done", "pipeline": {"state": "running", "position": 0},
                            "monitor_meta": {"clips": [{"n": 1}]}, "void": False,
                            "log_error": False, "log": [], "started": None, "last_activity": None,
                            "chat_last_ts": None, "spike_rows_session": [], "ended_at": None}, now=NOW)
    assert cs["state"] == "processando"


def test_session_spikes_midnight():
    """A spike from 23:59 belongs to the previous session when started is 00:05 (Review Focus 5)."""
    rows = [["2026-11-19 23:59:10", "1:02", "50", "gta", "spike_0.mp4"],
            ["2026-11-20 00:31:00", "3:20", "61", "carro", "spike_1.mp4"]]
    kept = server.session_spikes(rows, "2026-11-20T00:05:00+00:00")
    assert [r[1] for r in kept] == ["3:20"]


def test_session_spikes_no_started():
    rows = [["2026-11-20 00:31:00", "3:20", "61", "carro", "spike_1.mp4"]]
    assert server.session_spikes(rows, None) == rows
