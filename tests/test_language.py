import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from cut import load_transcript, sentences, translate_sentences, build_ass_sentences


def _job(tmp_path, payload):
    (tmp_path / "transcript.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_load_transcript_dict_format(tmp_path):
    d = _job(tmp_path, {"language": "es", "words": [{"w": "Hola", "s": 0.0, "e": 0.5}]})
    lang, words = load_transcript(d)
    assert lang == "es" and words[0]["w"] == "Hola"


def test_load_transcript_legacy_list(tmp_path):
    d = _job(tmp_path, [{"w": "Hola", "s": 0.0, "e": 0.5}])
    lang, words = load_transcript(d)
    assert lang is None and len(words) == 1


def test_sentences_breaks_on_pause_and_punct():
    words = [{"w": "Voy", "s": 0.0, "e": 0.3}, {"w": "ya.", "s": 0.4, "e": 0.7},
             {"w": "Listo", "s": 2.0, "e": 2.3}, {"w": "vamos!", "s": 2.4, "e": 2.8}]
    s = sentences(words)
    assert len(s) == 2 and s[0][-1]["w"] == "ya." and s[1][0]["w"] == "Listo"


def test_translate_sentences_fallback(monkeypatch):
    import cut
    monkeypatch.setattr(cut, "translator", lambda lang: (_ for _ in ()).throw(RuntimeError("net down")))
    evs = cut.translate_sentences([{"w": "Hola.", "s": 0.0, "e": 0.5}], "pt", 0.0, 1.0)
    assert evs[0]["text"].startswith("Hola") and evs[0]["e"] > evs[0]["s"]


def test_build_ass_sentences_edges():
    evs = [{"text": "Uma frase bem comprida, com virgulas, para forcar a quebra em dois eventos distintos aqui.", "s": 10.0, "e": 16.0},
           {"text": "Ok.", "s": 16.5, "e": 16.6}]
    ass = build_ass_sentences(evs, s0=10.0, cap_y=1560)
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    starts = [ln.split(",")[1] for ln in lines]
    assert len(lines) >= 3 and len(set(starts)) == len(starts)  # long one splits, times unique


def test_build_ass_sentences_masks_slurs():
    evs = [{"text": "the nigger word is masked", "s": 0.0, "e": 1.0}]
    ass = build_ass_sentences(evs, s0=0.0, cap_y=1560)
    assert "n*****" in ass and "nigger" not in ass


from cut import ass_hdr, ass_color, build_ass


def test_ass_hdr_defaults_keep_shipped_look():
    # the default palette must byte-match the styles that shipped before colours were configurable
    assert "Style: Cap,Arial Black,92,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000" in ass_hdr()
    assert "Style: Hook,Arial Black,68,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF" in ass_hdr()
    assert "Style: TitleBox,Arial Black,66,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF" in ass_hdr()
    assert "Style: TitleOut,Arial Black,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000" in ass_hdr()
    assert "Style: Bubble,Arial,58,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF" in ass_hdr()


def test_ass_color_rgb_to_bgr():
    assert ass_color("#FFFFFF") == "&H00FFFFFF" and ass_color("#000000") == "&H00000000"
    assert ass_color("#FFE500") == "&H0000E5FF" and ass_color("#FF0000") == "&H000000FF"


def test_palette_flows_into_word_highlight(tmp_path):
    words = [{"w": "olha", "s": 0.0, "e": 0.4}, {"w": "isso!", "s": 0.4, "e": 0.9}]
    ass = build_ass(words, 0.0, 1.0, "", 1560, palette={"cap": "#FF0000", "highlight": "#00FF00"})
    assert "Style: Cap,Arial Black,92,&H000000FF,&H0000FF00" in ass  # red text, green karaoke
    assert "{\\c&H0000FF00&}OLHA{\\c&H000000FF&}" in ass  # active word highlight tag follows too


def test_ass_hdr_invalid_color_exits():
    import pytest
    with pytest.raises(SystemExit):
        ass_hdr(cap="vermelho")


import argparse
from cut import resolve_lang, log_hook


def _a(**kw):
    base = dict(lang="", captions_lang="", captions="word")
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_native_default():
    assert resolve_lang(_a(), "es") == {"mode": "word", "lang_target": "es", "warnings": []}


def test_resolve_lang_pt_on_es():
    r = resolve_lang(_a(lang="pt"), "es")
    assert r["mode"] == "sentence" and r["lang_target"] == "pt"


def test_resolve_alias_and_conflict():
    r = resolve_lang(_a(captions_lang="pt-br"), "es")
    assert r["mode"] == "sentence" and r["lang_target"] == "pt"
    r2 = resolve_lang(_a(lang="de", captions_lang="pt"), "es")
    assert r2["lang_target"] == "de" and any("captions-lang" in w for w in r2["warnings"])


def test_resolve_none_wins_and_unknown_source():
    assert resolve_lang(_a(lang="pt", captions="none"), "es")["mode"] == "none"
    assert resolve_lang(_a(lang="pt"), None)["mode"] == "sentence"


def test_resolve_lang_equal_source_keeps_word():
    assert resolve_lang(_a(lang="es"), "es")["mode"] == "word"


def test_log_hook_appends_row(tmp_path):
    log_hook("job-1", 5, "pt", "Meu hook | teste", root=tmp_path)
    log_hook("job-2", 1, None, "Outro", root=tmp_path)
    txt = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "| job-1 | 5 | pt | Meu hook / teste |" in txt
    assert "| job-2 | 1 | - | Outro |" in txt


from moments import inherit_source_info


def test_inherit_from_live_job(tmp_path):
    live = tmp_path / "illojuan-e25bbbbb"
    (live / "live").mkdir(parents=True)
    (live / "live.json").write_text(json.dumps({"url": "https://twitch.tv/illojuan", "spikes": 0, "minutes": 3}), encoding="utf-8")
    spike = live / "live" / "spike_1.mp4"
    spike.write_bytes(b"x")
    info = inherit_source_info(str(spike), tmp_path)
    assert info["webpage_url"] == "https://twitch.tv/illojuan" and info["channel"] == "illojuan"


def test_inherit_none_for_regular_source(tmp_path):
    f = tmp_path / "video.mp4"; f.write_bytes(b"x")
    assert inherit_source_info(str(f), tmp_path) is None


from translate import write_variant_meta


def test_variant_meta_schema(tmp_path):
    base = {"layout": "react", "lang_source": "es", "lang_target": "pt", "hook": "x"}
    meta = write_variant_meta(tmp_path, 5, "de", base)
    assert meta["lang_target"] == "de" and meta["variant_of"] == "clip_5" and meta["layout"] == "react"
    assert json.loads((tmp_path / "clip_5_de.json").read_text(encoding="utf-8")) == meta


sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
from server import lang_label, build_retitle_cmd


def test_lang_label():
    assert lang_label({"lang_source": "es", "lang_target": "pt"}) == "ES→PT"
    assert lang_label({"lang_target": "es"}) == "ES"
    assert lang_label({}) == "?"


def test_build_retitle_cmd_modes(tmp_path):
    meta_word = {"start": 12.0, "end": 42.0, "layout": "blur", "cam": "0.1:0.2:0.3:0.4",
                 "lang_target": "es", "captions_mode": "word", "captions_style": "bubble", "hook": "h"}
    cmd = build_retitle_cmd(meta_word, tmp_path, "5", "Novo titulo", tmp_path / "t.json")
    j = " ".join(cmd)
    assert "--lang" not in cmd and "--translations-file" not in cmd
    assert "--captions bubble" in j and "--cam 0.1:0.2:0.3:0.4" in j
    assert "--hook" not in j  # hook only when explicitly passed by the caller
    meta_sent = dict(meta_word, layout="react", captions_mode="sentence", lang_target="pt",
                     sentences_cache=[{"text": "oi", "s": 12.0, "e": 13.0}], allow_profanity=True)
    cmd2 = build_retitle_cmd(meta_sent, tmp_path, "5", "Novo titulo", tmp_path / "t2.json")
    j2 = " ".join(cmd2)
    assert "--lang pt" in j2 and "--translations-file" in j2 and "--allow-profanity" in j2 and "--cam auto" in j2
