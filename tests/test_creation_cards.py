import json, os, sys, time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _job(tmp_path, name, cands, mtime=None, source=False):
    jd = tmp_path / "jobs" / name
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "candidates.json").write_text(json.dumps({
        "candidates": cands, "source": str(jd / "source.mp4") if source else ""}), encoding="utf-8")
    if source:
        (jd / "source.mp4").write_bytes(b"x")
    ts = mtime or time.time()
    os.utime(jd, (ts, ts))
    if (jd / "candidates.json").exists():
        os.utime(jd / "candidates.json", (ts, ts))  # current ranks by candidates mtime (review I2)
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server.common, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "ROOT", tmp_path)  # source_path is stored relative to ROOT


C1 = {"n": 1, "start": 10.0, "end": 40.0, "score": 3.0, "text": "primeiro momento",
      "llm": {"total_score": 62, "hook_title": "não vai acreditar", "hook_type": "afirmação"}}
C2 = {"n": 2, "start": 100.0, "end": 150.0, "score": 8.0, "text": "olha o carro!!!",
      "llm": {"total_score": 78, "hook_title": "olha o carro!!!", "hook_type": "contraste"}}
C3 = {"n": 3, "start": 200.0, "end": 230.0, "score": 9.0, "text": ""}  # sem LLM


def test_moments_sorted_by_score(tmp_path, monkeypatch):
    """Best LLM score first (spec D2) — same rule as the pipeline: llm.total_score
    replaces the heuristic when present (monitor.py's own sort)."""
    _redirect(tmp_path, monkeypatch)
    _job(tmp_path, "job-a", [C1, C2, C3], source=True)
    j = server.creation_info()[0]
    ms = j["moments"]
    assert [m["n"] for m in ms] == [2, 1, 3]  # llm 78 > llm 62 > heurístico 9
    assert ms[0]["hook"] == "olha o carro!!!"
    assert ms[2]["hook"].startswith("Momento 3")  # no llm title, no text → fallback


def test_current_flag_most_recent_with_candidates(tmp_path, monkeypatch):
    old = time.time() - 7200
    _redirect(tmp_path, monkeypatch)
    _job(tmp_path, "job-old", [C1], mtime=old)
    _job(tmp_path, "job-new", [C2], mtime=time.time())
    _job(tmp_path, "job-empty", [], mtime=time.time() - 60)  # sem candidatos: nunca é current
    jobs = {j["id"]: j for j in server.creation_info()}
    assert jobs["job-new"]["current"] is True
    assert jobs["job-old"]["current"] is False
    assert jobs["job-empty"]["current"] is False


def test_thumb_url_and_kind(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _job(tmp_path, "job-a", [C1], source=True)
    j = server.creation_info()[0]
    m = j["moments"][0]
    assert m["thumb"] == "/api/moment-thumb?job=job-a&n=1"
    assert m["thumb_kind"] == "source"
    assert j["source_path"] == "jobs/job-a/source.mp4"  # the preview player needs the video


def test_thumb_none_when_source_cleaned(tmp_path, monkeypatch):
    """No source and no cut → thumb null: the card goes gray, never shows a borrowed image."""
    _redirect(tmp_path, monkeypatch)
    _job(tmp_path, "job-cleaned", [C1], source=False)
    j = server.creation_info()[0]
    assert j["moments"][0]["thumb"] is None and j["moments"][0]["thumb_kind"] is None
    assert j["source_available"] is False


def test_pick_marks_best_when_auto(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path, "job-a", [C1, C2], source=True)
    (jd / "creation.json").write_text(json.dumps(
        {"status": "ready", "options": {"mode": "auto"}}), encoding="utf-8")
    j = server.creation_info()[0]
    picks = [m["n"] for m in j["moments"] if m.get("pick")]
    assert picks == [2]  # best score


def test_current_uses_candidates_mtime_not_dir(tmp_path, monkeypatch):
    """Extracting thumbs touches the job DIR's mtime — it must NOT hijack `current`
    (review I2): the rank is the candidates.json mtime."""
    now = time.time()
    _redirect(tmp_path, monkeypatch)
    # A: DIR touched NOW (thumb extraction), candidates OLDER; B: candidates NEWER, dir old
    jd_a = _job(tmp_path, "job-a", [C1], mtime=now - 7200)
    _job(tmp_path, "job-b", [C2], mtime=now - 3600)
    (jd_a / "thumbs").mkdir()
    os.utime(jd_a, (now, now))
    jobs = {j["id"]: j for j in server.creation_info()}
    assert jobs["job-b"]["current"] is True  # by candidates.json: B; by dir: A (wrong rule)


def test_clip_info_skips_thumb_while_pipeline_renders(tmp_path, monkeypatch):
    """The D0 pipeline renders its own thumbs — a dashboard-side ffmpeg on the same output
    file races it and fails the whole cut step (bug seen 2026-11-20, review of logs)."""
    _redirect(tmp_path, monkeypatch)
    jd = tmp_path / "jobs" / "job-render"
    ed = jd / "edits"
    ed.mkdir(parents=True)
    (ed / "clip_1.mp4").write_bytes(b"x")
    (ed / "clip_1.json").write_text(json.dumps({"start": 10, "end": 40}), encoding="utf-8")
    (jd / "creation.json").write_text(json.dumps({"status": "rendering", "pid": 123}), encoding="utf-8")
    monkeypatch.setattr(server, "pid_alive", lambda pid: pid == 123)  # pipeline process alive
    n = {"c": 0}
    monkeypatch.setattr(server, "ensure_thumb", lambda mp4, target: n.__setitem__("c", n["c"] + 1) or target)
    clips = server.clip_info(jd)
    assert n["c"] == 0  # no dashboard ffmpeg while the pipeline owns the job
    assert clips[0]["thumb"] is None


def test_clip_info_makes_thumb_when_idle(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = tmp_path / "jobs" / "job-idle"
    ed = jd / "edits"
    ed.mkdir(parents=True)
    (ed / "clip_1.mp4").write_bytes(b"x")
    (ed / "clip_1.json").write_text(json.dumps({"start": 10, "end": 40}), encoding="utf-8")
    n = {"c": 0}
    monkeypatch.setattr(server, "ensure_thumb",
                        lambda mp4, target: n.__setitem__("c", n["c"] + 1) or Path(str(target)))
    clips = server.clip_info(jd)
    assert n["c"] == 1 and clips[0]["thumb"]


def test_clip_info_no_recursion_with_auto_mode(tmp_path, monkeypatch):
    """clip_info reads creation.json directly: creation_run() calls clip_info() back for
    auto mode — through the old path this recursed to death (found live 2026-11-21)."""
    _redirect(tmp_path, monkeypatch)
    jd = tmp_path / "jobs" / "job-auto"
    ed = jd / "edits"
    ed.mkdir(parents=True)
    (ed / "clip_1.mp4").write_bytes(b"x")
    (ed / "clip_1.json").write_text(json.dumps({"start": 10, "end": 40}), encoding="utf-8")
    (jd / "creation.json").write_text(json.dumps(
        {"status": "ready", "options": {"mode": "auto"}, "pid": 0}), encoding="utf-8")
    clips = server.clip_info(jd)  # must simply not recurse
    assert len(clips) == 1
