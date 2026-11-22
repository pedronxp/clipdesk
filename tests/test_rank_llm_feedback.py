import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import rank_llm

CANDS = [{"n": 1, "_cid": "c1", "start": 10, "end": 40, "score": 5.0, "text": "olha isso", "why": ["audio spike"]}]
INFO = {"channel": "debug", "url": "https://twitch.tv/debug"}
PERF = "Histórico de performance do criador: ..."


def test_fb_ctx_appended():
    prompt = rank_llm.build_user_prompt(CANDS, INFO, PERF, "Picos marcados como FALSO POSITIVO: ...")
    assert "FALSO POSITIVO" in prompt
    assert PERF in prompt  # perf context still present, not replaced


def test_fb_ctx_none_keeps_prompt_identical():
    base = rank_llm.build_user_prompt(CANDS, INFO, PERF)
    same = rank_llm.build_user_prompt(CANDS, INFO, PERF, None)
    assert base == same
