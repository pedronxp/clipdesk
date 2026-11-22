import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from voids import source_referenced, is_void


def _void_candidate(tmp_path, name="a-11111111"):
    d = tmp_path / name
    (d / "live").mkdir(parents=True)
    (d / "live" / "seg_00000.ts").write_bytes(b"x")  # blocks and chat, but no video anywhere
    return d


def test_source_referenced_blocks_void(tmp_path):
    a = _void_candidate(tmp_path)
    b = tmp_path / "b-22222222"
    b.mkdir()
    (b / "candidates.json").write_text(
        json.dumps({"source": str(a / "illojuan.mp4")}), encoding="utf-8")
    assert source_referenced(a, tmp_path) is True
    assert is_void(a, running=False, jobs_root=tmp_path) is False


def test_unreferenced_job_still_void(tmp_path):
    a = _void_candidate(tmp_path)
    assert source_referenced(a, tmp_path) is False
    assert is_void(a, running=False, jobs_root=tmp_path) is True


def test_own_candidates_do_not_count(tmp_path):
    a = _void_candidate(tmp_path)
    (a / "candidates.json").write_text(
        json.dumps({"source": str(a / "illojuan.mp4")}), encoding="utf-8")
    assert source_referenced(a, tmp_path) is False
