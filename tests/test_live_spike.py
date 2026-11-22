import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from live import ChatTail, HYPE


def test_hype_recognizes_brazilian_laughter():
    """207 msgs of kkk in the peak window counted 4 hype points before (bug 2026-11-21)."""
    assert HYPE.search("kkkkkkkkkkkkkkk")
    assert HYPE.search("hahahahaha demais")
    assert HYPE.search("KKKK algo engraçado")   # caps: the (?i:) scope covers the laughter runs
    assert HYPE.search("morri 😂🤣")
    assert not HYPE.search("bom dia pessoal")


def test_tail_tracks_message_time_not_machine_time(tmp_path):
    """The newest accepted message second drives the spike window — YouTube chat arrives
    late with server timestamps (bug: the machine-now window was chronologically empty)."""
    tail = ChatTail(tmp_path, 1000)
    line = {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"id": "m1", "timestampUsec": str(1_795_360_000 * 1_000_000),
        "message": {"runs": [{"text": "kkkkkkkk"}]}}}}}]}}
    (tmp_path / "chat.live_chat.json.part").write_text(json.dumps(line) + "\n", encoding="utf-8")
    tail.poll()
    assert tail.last_ts == 1_795_360_000
    assert tail.wgt[1_795_360_000] == 2  # kkk counts double now


def test_tail_ignores_backlog_before_start(tmp_path):
    """History backfill (old timestamps) must not count as fresh chat."""
    tail = ChatTail(tmp_path, 1_795_360_000)
    old = {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"id": "m0", "timestampUsec": str(1_795_350_000 * 1_000_000),
        "message": {"runs": [{"text": "oi"}]}}}}}]}}
    (tmp_path / "chat.live_chat.json.part").write_text(json.dumps(old) + "\n", encoding="utf-8")
    tail.poll()
    assert tail.last_ts == 0 and not tail.raw
