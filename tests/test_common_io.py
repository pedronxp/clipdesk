import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from common import load_json, save_json


def test_save_json_is_atomic(tmp_path):
    """Regression: save_json used to truncate the target before serializing, so a crash or
    two writers racing on monitor.json left a 0-byte file and every reader blew up with
    'Expecting value: line 1 column 1 (char 0)'."""
    p = tmp_path / "monitor.json"
    save_json(p, {"clips": [{"n": 1}]})
    assert load_json(p) == {"clips": [{"n": 1}]}
    assert not (tmp_path / "monitor.json.tmp").exists()

    try:
        save_json(p, {"bad": object()})  # unserializable payload
    except TypeError:
        pass
    else:
        raise AssertionError("an unserializable payload must raise")

    assert load_json(p) == {"clips": [{"n": 1}]}  # previous content survived
    assert not (tmp_path / "monitor.json.tmp").exists()
