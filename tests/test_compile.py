import json, shutil, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import compile as c


def cand(n, start, end, peak=None, score=10.0, llm_total=None, hook=None):
    return {"n": n, "start": start, "end": end, "peak": peak, "score": score,
            "text": f"fala do momento {n}",
            "llm": {"total_score": llm_total if llm_total is not None else score,
                    "hook_title": hook or f"Título {n}"}}


def test_select_moments_fits_target_caps_long_windows_and_orders_chronologically():
    cands = [cand(1, 0, 90, peak=50, llm_total=49),      # 90 s
             cand(2, 100, 190, peak=150, llm_total=23),  # 90 s
             cand(3, 200, 350, peak=280, llm_total=35),  # 150 s → cap 120 s em torno do pico
             cand(4, 330, 400, peak=360, llm_total=12)]  # 70 s — não cabe mais no alvo
    picked = c.select_moments(cands, 300.0)
    assert [p["n"] for p in picked] == [1, 2, 3]  # ordem cronológica no vídeo final
    # o momento 3 entra aparado simetricamente no pico (280): [220, 340]
    windows = {p["n"]: c.window_of(p) for p in cands}
    assert windows[3][:2] == pytest.approx((220.0, 340.0))
    total = sum(c.window_of(p)[2] for p in picked)
    assert total == pytest.approx(300.0)


def test_manual_selection_uses_the_listed_ns_and_fails_when_none_matches():
    cands = [cand(1, 0, 90), cand(3, 200, 350, peak=280), cand(9, 400, 460)]
    picked = c.select_moments(cands, 600.0, manual="3,1")
    assert [p["n"] for p in picked] == [1, 3]  # sempre cronológico, n desconhecido cai fora
    with pytest.raises(SystemExit, match="nenhum dos momentos"):
        c.select_moments(cands, 600.0, manual="7,8")


def test_auto_selection_still_takes_the_best_even_when_it_overflows_the_target():
    cands = [cand(1, 0, 200, peak=100, llm_total=40)]  # aparado a 120 s > alvo de 2 min? não: alvo é 300
    picked = c.select_moments(cands, 300.0)
    assert [p["n"] for p in picked] == [1]


def test_pip_covers_the_detected_cam_and_none_without_a_cam(monkeypatch):
    class Facecam:
        @staticmethod
        def find_cam(video, s0, e0):
            return {"mode": "cam", "box": [0.6, 0.1, 0.2, 0.25], "reason": "moldura limpa"}

    monkeypatch.setitem(sys.modules, "facecam", Facecam)
    layout = c.pip_layout("source.mp4", 0, 90)
    # caixa 0.2x1920=384 x 0.25x1080=270 → PiP no mínimo 480x270, cobrindo a webcam original
    assert "crop=iw*0.2000:ih*0.2500:iw*0.6000:ih*0.1000" in layout["crop"]
    assert "scale=480:278" in layout["crop"] and "drawbox" in layout["crop"]  # borda de destaque
    assert layout["x"] == 1148 and layout["y"] == 104

    class HugeCam:
        @staticmethod
        def find_cam(video, s0, e0):
            return {"mode": "cam", "box": [0.72, 0.02, 0.26, 0.28]}

    monkeypatch.setitem(sys.modules, "facecam", HugeCam)
    layout = c.pip_layout("source.mp4", 0, 90)
    # webcam queimada maior que o mínimo: o PiP cresce para cobrir (508x310, +margem)
    assert "scale=508:310" in layout["crop"]
    assert layout["x"] == 1378 and layout["y"] == 18

    class NoCam:
        @staticmethod
        def find_cam(video, s0, e0):
            return {"mode": "none", "reason": "no face stable across frames"}

    monkeypatch.setitem(sys.modules, "facecam", NoCam)
    assert c.pip_layout("source.mp4", 0, 90) is None


def test_moment_cmd_has_fades_loudnorm_and_identical_encoders():
    without = c.moment_cmd("src.mp4", 10.0, 100.0, "out/mom_1.mp4")
    graph = without[without.index("-filter_complex") + 1]
    # o vídeo vai SEMPRE por -filter_complex com -map [vo] explícito: um -map 0:a? sozinho
    # desliga a seleção automática e o vídeo não entra no arquivo (bug do primeiro smoke test)
    assert graph.startswith("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080")
    assert "fade=t=in:st=0:d=0.5" in graph and "fade=t=out:st=89.50:d=0.5" in graph
    assert graph.endswith("[vo]")
    assert without[without.index("-map") + 1] == "[vo]"
    af = without[without.index("-af") + 1]
    assert af == "loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.5,afade=t=out:st=89.50:d=0.5"
    assert without[without.index("-t") + 1] == "90.00"

    with_pip = c.moment_cmd("src.mp4", 10.0, 100.0, "out/mom_2.mp4",
                            {"crop": "crop=iw*0.2:ih*0.25:iw*0.6:ih*0.1", "x": 1148, "y": 104})
    graph = with_pip[with_pip.index("-filter_complex") + 1]
    assert "[0:v]split[base][camsrc]" in graph and "overlay=1148:104" in graph
    assert with_pip[with_pip.index("-map") + 1] == "[vo]"
    # codificação idêntica entre momentos: concat demuxer pode juntar com -c copy
    for flag_value in (("-c:v", "libx264"), ("-preset", "medium"), ("-crf", "20"),
                       ("-pix_fmt", "yuv420p"), ("-c:a", "aac"), ("-b:a", "160k"), ("-ar", "48000")):
        for cmd in (without, with_pip):
            i = cmd.index(flag_value[0])
            assert cmd[i + 1] == flag_value[1]


def test_resolve_source_prefers_the_reference_then_the_job_video(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    data = {"source": str(tmp_path / "gone.mp4")}
    (job / "live").mkdir()
    (job / "live" / "source_cfr.mp4").write_bytes(b"x")
    assert c.resolve_source(job, data) == job / "live" / "source_cfr.mp4"
    shutil.rmtree(job / "live")
    (job / "clip_3.mp4").write_bytes(b"x")  # cortes renderizados nunca são a fonte
    (job / "outro_video.mp4").write_bytes(b"x")
    assert c.resolve_source(job, data) == job / "outro_video.mp4"


def test_run_compile_renders_moments_concats_and_writes_the_kit(tmp_path, monkeypatch):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"video")
    job = tmp_path / "jobs" / "job-1"
    job.mkdir(parents=True)
    (job / "candidates.json").write_text(json.dumps({
        "source": str(src),
        "info": {"title": "Live de GTA 6", "webpage_url": "https://www.youtube.com/watch?v=abc",
                 "channel": "Canal X"},
        "duration": 400,
        "candidates": [cand(1, 0, 90, peak=50, llm_total=49), cand(2, 100, 190, peak=150, llm_total=23),
                       cand(3, 200, 350, peak=280, llm_total=35)]}, ensure_ascii=False), encoding="utf-8")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"rendered")  # todo ffmpeg termina no arquivo de saída

    monkeypatch.setattr(c, "run", fake_run)
    monkeypatch.setattr(c, "tool", lambda name: name)
    monkeypatch.setattr(c, "ffprobe", lambda path, at=None: {"format": {"duration": "600.0"}})
    monkeypatch.setattr(c, "ROOT", tmp_path)

    c.run_compile(job, {"pip": False, "target_min": 10})

    renders = [cmd for cmd in cmds if "-filter_complex" in cmd]  # a thumb usa -vf, o concat não usa filtro
    assert len(renders) == 3
    assert [renders[i][renders[i].index("-ss") + 1] for i in range(3)] == ["0.00", "100.00", "220.00"]
    concat = cmds[3]
    assert concat[concat.index("-f") + 1] == "concat" and "-c" in concat and "copy" in concat
    thumb = cmds[4]
    assert thumb[thumb.index("-frames:v") + 1] == "1" and thumb[-1].endswith("thumb.jpg")

    plan = json.loads((job / "compile" / "plan.json").read_text(encoding="utf-8"))
    assert [(m["n"], m["start"], m["end"]) for m in plan["moments"]] == [(1, 0.0, 90.0), (2, 100.0, 190.0), (3, 220.0, 340.0)]
    assert not list((job / "compile").glob("mom_*.mp4"))  # intermediatos apagados após o concat

    state = json.loads((job / "compile.json").read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["output"] == "jobs/job-1/compile/melhores.mp4"
    assert state["title"] == "MELHORES MOMENTOS — Canal X"
    assert state["kit"] == "out/job-1-melhores"
    assert state["moments"] == 3 and state["duration"] == 600.0

    kit = tmp_path / "out" / "job-1-melhores"
    for name in ("melhores.mp4", "thumb.jpg", "legendas.md"):
        assert (kit / name).exists(), name
    leg = (kit / "legendas.md").read_text(encoding="utf-8")
    assert "Fonte: https://www.youtube.com/watch?v=abc" in leg
    assert "0:00 Momento 1 — Título 1" in leg
    assert "1:30 Momento 2 — Título 2" in leg
    assert "3:00 Momento 3 — Título 3" in leg


def test_run_compile_skips_a_failed_moment_and_still_ships_the_rest(tmp_path, monkeypatch):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"video")
    job = tmp_path / "jobs" / "job-2"
    job.mkdir(parents=True)
    (job / "candidates.json").write_text(json.dumps({
        "source": str(src), "info": {"channel": "Canal Y"}, "duration": 400,
        "candidates": [cand(1, 0, 90, peak=50, llm_total=49), cand(2, 200, 290, peak=240, llm_total=20)]},
        ensure_ascii=False), encoding="utf-8")

    def fake_run(cmd, **kw):
        if "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "200.00":
            raise SystemExit("command failed (1): momento corrompido")
        Path(cmd[-1]).write_bytes(b"rendered")

    monkeypatch.setattr(c, "run", fake_run)
    monkeypatch.setattr(c, "tool", lambda name: name)
    monkeypatch.setattr(c, "ffprobe", lambda path, at=None: {"format": {"duration": "90.0"}})
    monkeypatch.setattr(c, "ROOT", tmp_path)

    c.run_compile(job, {"pip": False})
    state = json.loads((job / "compile.json").read_text(encoding="utf-8"))
    assert state["status"] == "ready" and state["moments"] == 1
    assert "Momento 1" in state["description"] and "Momento 2" not in state["description"]


def test_server_start_compile_guards_and_lifecycle(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
    import server
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(server, "JOBS", jobs)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "LOGS", tmp_path / "logs")

    (jobs / "job-1").mkdir()
    with pytest.raises(RuntimeError, match="analise esta fonte"):
        server.start_compile("job-1", {})
    (jobs / "job-1" / "candidates.json").write_text("{}", encoding="utf-8")

    spawned = []

    class FakeProc:
        pid = 4242

    def fake_spawn(cmd, fh, env):
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(server, "_spawn_detached", fake_spawn)
    data = server.start_compile("job-1", {"target_min": 10, "pip": False})
    assert data["status"] == "running" and data["pid"] == 4242
    assert spawned[0][-1] == json.dumps({"target_min": 10, "pip": False}, ensure_ascii=False)
    assert json.loads((jobs / "job-1" / "compile.json").read_text(encoding="utf-8"))["status"] == "running"

    monkeypatch.setattr(server, "pid_alive", lambda pid: pid == 4242)
    with pytest.raises(RuntimeError, match="já estão sendo gerados"):
        server.start_compile("job-1", {})

    compile_json = jobs / "job-1" / "compile.json"
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    out = tmp_path / "jobs" / "job-1" / "compile" / "melhores.mp4"
    out.parent.mkdir(parents=True)
    # pid morto e sem output declarado → failed (reconciliado no próprio arquivo)
    compile_json.write_text(json.dumps({"pid": 4242, "status": "running"}), encoding="utf-8")
    st = server.compile_run(jobs / "job-1")
    assert st["status"] == "failed"
    # estado de processo morto com vídeo pronto → ready (o campo output aponta para jobs/)
    compile_json.write_text(json.dumps({"pid": 4242, "status": "running",
                                        "output": "jobs/job-1/compile/melhores.mp4"}), encoding="utf-8")
    out.write_bytes(b"final")
    st = server.compile_run(jobs / "job-1")
    assert st["status"] == "ready" and st["pid"] == 0


def test_moment_cmd_generalizes_fps_sar_stereo_vertical_and_silent():
    cmd = c.moment_cmd("src.mp4", 10.0, 100.0, "out/m.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "fps=30,setsar=1" in graph and graph.endswith("[vo]")  # concat -c copy exige uniformidade
    assert cmd[cmd.index("-ac") + 1] == "2"

    silent = c.moment_cmd("src.mp4", 0.0, 30.0, "out/s.mp4", silent=True)
    assert "anullsrc=r=48000:cl=stereo" in " ".join(silent)
    maps = [silent[i + 1] for i, part in enumerate(silent) if part == "-map"]
    assert maps == ["[vo]", "1:a"]  # vídeo do filtro, áudio sintetizado

    vert = c.moment_cmd("src.mp4", 0.0, 30.0, "out/v.mp4", vertical=True)
    g = vert[vert.index("-filter_complex") + 1]
    assert "boxblur=20:2" in g and "overlay=(W-w)/2:(H-h)/2" in g and g.endswith("[vo]")


def test_probe_media_assumes_landscape_with_audio_without_streams():
    # ffprobe sem "streams" (mock do suíte ou probe indisponível) → paisagem com áudio
    assert c.probe_media("x.mp4") == (False, False)


def test_run_compile_multi_plan_uses_each_moments_own_source(tmp_path, monkeypatch):
    src_a, src_b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src_a.write_bytes(b"a"); src_b.write_bytes(b"b")
    jobs = tmp_path / "jobs"
    j_a, j_b = jobs / "job-a", jobs / "job-b"
    for jd, src, ch in ((j_a, src_a, "Canal A"), (j_b, src_b, "Canal B")):
        jd.mkdir(parents=True)
        (jd / "candidates.json").write_text(json.dumps({"source": str(src), "info": {"channel": ch},
            "candidates": [cand(1, 0, 60, peak=30, llm_total=80), cand(2, 100, 160, peak=130, llm_total=60)]}),
            encoding="utf-8")
    viral = jobs / "viral-20261121-210000"
    viral.mkdir()
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"rendered")

    monkeypatch.setattr(c, "run", fake_run)
    monkeypatch.setattr(c, "tool", lambda name: name)
    monkeypatch.setattr(c, "ffprobe", lambda path, at=None: {"format": {"duration": "600.0"}})
    monkeypatch.setattr(c, "ROOT", tmp_path)

    c.run_compile(viral, {"multi": True, "pip": False,
                          "moments_plan": [{"job": "job-a", "n": 1}, {"job": "job-b", "n": 1},
                                           {"job": "job-a", "n": 2}]})
    renders = [cmd for cmd in cmds if "-filter_complex" in cmd]
    assert len(renders) == 3  # ordem escolhida pelo operador, cada momento com sua fonte
    assert [r[r.index("-i") + 1] for r in renders] == [str(src_a), str(src_b), str(src_a)]

    plan = json.loads((viral / "compile" / "plan.json").read_text(encoding="utf-8"))
    assert plan["multi"] is True
    assert [e["job"] for e in plan["entries"]] == ["job-a", "job-b", "job-a"]

    state = json.loads((viral / "compile.json").read_text(encoding="utf-8"))
    assert state["status"] == "ready" and state["multi"] is True
    assert state["output"] == "jobs/viral-20261121-210000/compile/melhores.mp4"
    assert state["title"].startswith("VÍDEO VIRAL — Canal A")  # canais agregados dos dois jobs
    kit = tmp_path / "out" / "viral-20261121-210000"
    assert (kit / "legendas.md").exists()
    assert "Canal A, Canal B" in (kit / "legendas.md").read_text(encoding="utf-8")
