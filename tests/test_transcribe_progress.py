import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common


def test_progress_every_minute():
    """One line per completed audio-minute, only when the minute is new (spec D8)."""
    st = {}
    assert common.transcribe_progress(30, 120, st) is None   # 30 s: no minute complete
    assert common.transcribe_progress(70, 120, st) == "transcrição 1/2 min"
    assert common.transcribe_progress(75, 120, st) is None   # still inside minute 1
    assert common.transcribe_progress(130, 120, st) == "transcrição 2/2 min"


def test_progress_zero_total():
    st = {}
    assert common.transcribe_progress(50, 0, st) is None     # unknown duration: never lie


def test_progress_is_monotonic():
    st = {}
    assert common.transcribe_progress(10, 600, st) is None
    assert common.transcribe_progress(5, 600, st) is None    # segment ordering hiccup
    assert common.transcribe_progress(65, 600, st) == "transcrição 1/10 min"
