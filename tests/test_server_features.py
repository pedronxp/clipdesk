"""Rotas novas do painel (vídeo viral, liberar espaço em lote, apagar projeto, IA do YouTube)
testadas contra um servidor HTTP real em porta efêmera, com JOBS/ROOT isolados no tmp —
os projetos reais nunca são tocados."""
import json, sys, threading, urllib.error, urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
import server


@pytest.fixture()
def api(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(server, "JOBS", jobs)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "LOGS", logs)
    monkeypatch.setattr(server, "WATCHLIST", tmp_path / "watchlist.json")
    monkeypatch.setattr(server, "PIPELINE_QUEUE", tmp_path / "pipeline_queue.json")
    srv = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", jobs, tmp_path
    srv.shutdown()


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def fake_job(jobs, jid, media=True):
    """Projeto com 1 momento analisado e 1 corte pronto (clip_info precisa do .mp4 + thumb)."""
    jd = jobs / jid
    (jd / "edits").mkdir(parents=True)
    (jd / "candidates.json").write_text(json.dumps(
        {"source": str(jd / "src.mp4"),
         "info": {"channel": "Canal X", "title": "Live de GTA 6", "webpage_url": "https://youtu.be/x"},
         "candidates": [{"n": 1, "start": 0, "end": 60, "peak": 30, "score": 80, "text": "fala do momento"}]},
        ensure_ascii=False), encoding="utf-8")
    if media:
        (jd / "src.mp4").write_bytes(b"r" * 1_000_000)
        (jd / "audio.wav").write_bytes(b"w" * 1_000_000)
    (jd / "edits" / "clip_1.mp4").write_bytes(b"clip")
    (jd / "edits" / "thumb.jpg").write_bytes(b"t")
    (jd / "edits" / "clip_1.json").write_text(json.dumps(
        {"n": 1, "hook": "gancho do clipe", "start": 0, "end": 60, "orientation": "vertical"},
        ensure_ascii=False), encoding="utf-8")
    return jd


# ---------- vídeo viral ----------

def test_viral_route_validates_and_spawns_multi_plan(api, monkeypatch):
    base, jobs, root = api
    fake_job(jobs, "job-a")

    spawned = []

    class FakeProc:
        pid = 4321

    def fake_spawn(cmd, fh, env):
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(server, "_spawn_detached", fake_spawn)

    code, body = post(f"{base}/api/create/viral", {"moments": []})
    assert code == 400 and "ao menos um momento" in body["error"]

    code, body = post(f"{base}/api/create/viral", {"moments": [{"job": "job-z", "n": 1}]})
    assert code == 400 and "não tem momentos analisados" in body["error"]

    code, body = post(f"{base}/api/create/viral",
                      {"moments": [{"job": "job-a", "n": 1}, {"job": "job-a", "n": 1}],
                       "target_min": 5, "pip": False, "title": "Vídeo viral de hoje"})
    assert code == 202 and body["viral"]["status"] == "running"
    options = json.loads(spawned[0][-1])
    assert options["multi"] is True and options["moments_plan"] == [{"job": "job-a", "n": 1}]
    assert options["title"] == "Vídeo viral de hoje"
    viral_dir = next((jobs / "viral-").parent.glob("viral-*"))
    state = json.loads((viral_dir / "compile.json").read_text(encoding="utf-8"))
    assert state["status"] == "running" and state["multi"] is True

    # aparece em /api/state como canal próprio do viral
    code, state_all = get(f"{base}/api/state")
    ids = [v["id"] for v in state_all["viral"]]
    assert len(ids) == 1 and ids[0].startswith("viral-")
    # e NÃO vira projeto na aba Criar cortes
    assert all(not j["id"].startswith("viral-") for j in state_all["creation"])


def test_viral_reconciles_to_ready_when_the_output_exists(api, monkeypatch):
    base, jobs, root = api
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd, fh, env: type("P", (), {"pid": 77}))
    post(f"{base}/api/create/viral", {"moments": [{"job": "_", "n": 1}]}) if False else None
    # monta o viral "pronto" direto no disco (o spawn real não roda ffmpeg nos testes)
    vdir = jobs / "viral-20261121-220000"
    (vdir / "compile").mkdir(parents=True)
    (vdir / "compile" / "melhores.mp4").write_bytes(b"final")
    (vdir / "compile.json").write_text(json.dumps(
        {"pid": 77, "status": "running", "started": "2026-11-21T22:00:00+00:00",
         "output": "jobs/viral-20261121-220000/compile/melhores.mp4", "title": "Vídeo viral"}), encoding="utf-8")
    code, state = get(f"{base}/api/state")
    viral = state["viral"][0]["compile"]
    assert viral["status"] == "ready" and viral["pid"] == 0


# ---------- liberar espaço (esvaziar) ----------

def test_cleanup_all_preview_sums_projects_with_clips_and_media(api):
    base, jobs, root = api
    fake_job(jobs, "job-ok", media=True)
    fake_job(jobs, "job-cleaned", media=False)  # origem já liberada → fora da conta
    code, body = get(f"{base}/api/create/cleanup-all-preview")
    assert code == 200
    ids = [j["id"] for j in body["jobs"]]
    assert ids == ["job-ok"] and body["total_mb"] > 0


def test_cleanup_all_requires_confirm_and_runs_the_batch(api):
    base, jobs, root = api
    fake_job(jobs, "job-1", media=True)
    fake_job(jobs, "job-2", media=True)

    code, body = post(f"{base}/api/create/cleanup-all", {})
    assert code == 400 and "confirmação" in body["error"]

    code, body = post(f"{base}/api/create/cleanup-all", {"confirm": True})
    assert code == 200 and sorted(body["cleaned"]) == ["job-1", "job-2"]
    assert body["mb_freed"] > 0
    for jid in ("job-1", "job-2"):
        assert (jobs / jid / "src.mp4").exists() is False      # origem apagada
        assert (jobs / jid / "edits" / "clip_1.mp4").exists()  # cortes preservados
        assert json.loads((jobs / jid / "cleanup.json").read_text(encoding="utf-8"))["files_removed"] == 2


def test_jobs_delete_requires_confirm_guards_and_removes_the_folder(api):
    base, jobs, root = api
    fake_job(jobs, "job-del")

    code, body = post(f"{base}/api/jobs/delete", {"job": "job-del"})
    assert code == 400 and "confirmação" in body["error"]

    code, body = post(f"{base}/api/jobs/delete", {"job": "job-missing", "confirm": True})
    assert code == 404 and "não encontrado" in body["error"]

    code, body = post(f"{base}/api/jobs/delete", {"job": "../escape", "confirm": True})
    assert code == 400 and "inválido" in body["error"]

    code, body = post(f"{base}/api/jobs/delete", {"job": "job-del", "confirm": True})
    assert code == 200 and body["ok"] is True
    assert not (jobs / "job-del").exists()


# ---------- IA do YouTube ----------

def test_youtube_suggest_flow(api, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    base, jobs, root = api
    fake_job(jobs, "job-a")

    # sem LLM configurado → erro amigável (o guard vem antes de qualquer chamada)
    monkeypatch.setattr(server.common, "llm_config", lambda: None)
    code, body = post(f"{base}/api/youtube/suggest", {"file": "job-a/edits/clip_1.mp4"})
    assert code == 400 and "configure o LLM" in body["error"]

    # contexto montado a partir do corte e da compilação
    ctx = server.youtube_suggest_context("job-a/edits/clip_1.mp4")
    assert ctx and ctx["kind"] == "corte vertical" and ctx["hook"] == "gancho do clipe"
    assert server.youtube_suggest_context("nao/existe.mp4") is None
    (jobs / "viral-1" / "compile").mkdir(parents=True)
    (jobs / "viral-1" / "compile" / "melhores.mp4").write_bytes(b"f")
    (jobs / "viral-1" / "compile.json").write_text(json.dumps(
        {"status": "ready", "title": "VÍDEO VIRAL — Canal X", "duration": 612.3}), encoding="utf-8")
    ctx = server.youtube_suggest_context("jobs/viral-1/compile/melhores.mp4")
    assert ctx["kind"] == "compilação (melhores momentos)" and ctx["hook"] == "VÍDEO VIRAL — Canal X"

    # com LLM configurado: stub OpenAI-compatível local devolve JSON com fence
    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = json.dumps({"choices": [{"message": {"content":
                '```json\n{"title": "Gancho do clipe #shorts", "description": "Descrição IA.\\n#GTA6"}\n```'}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    stub = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    monkeypatch.setattr(server.common, "llm_config",
                        lambda: {"base": f"http://127.0.0.1:{stub.server_address[1]}", "key": "k", "model": "stub"})
    code, body = post(f"{base}/api/youtube/suggest", {"file": "job-a/edits/clip_1.mp4"})
    assert code == 200, body
    assert body["title"] == "Gancho do clipe #shorts"
    assert body["description"].startswith("Descrição IA.")
    stub.shutdown()
