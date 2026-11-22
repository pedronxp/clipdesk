import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import youtube_publish as yt


class Response:
    def __init__(self, status=200, data=None, **headers):
        self.status_code = status
        self.data = data or {}
        self.headers = headers

    def json(self):
        return self.data


def test_oauth_uses_one_time_state_and_never_exposes_tokens(tmp_path, monkeypatch):
    publisher = yt.YouTubePublisher(tmp_path / "state", tmp_path / "jobs")
    redirect = "http://127.0.0.1:8787/api/youtube/callback"
    config = {"installed": {"client_id": "desktop.apps.googleusercontent.com", "client_secret": "client-secret"}}
    query = parse_qs(urlsplit(publisher.authorize({"credentials": config}, redirect)).query)
    assert query["redirect_uri"] == [redirect]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query
    with pytest.raises(ValueError, match="inválida"):
        publisher.callback({"state": ["other"], "code": ["code"]})

    def google(method, url, **kwargs):
        if url == yt.TOKEN_URL:
            assert kwargs["data"]["redirect_uri"] == redirect
            assert kwargs["data"]["code_verifier"]
            return Response(data={"access_token": "access-token", "refresh_token": "refresh-token", "scope": " ".join(yt.SCOPES)})
        if url == yt.REVOKE_URL:
            assert kwargs["data"]["token"] == "refresh-token"
            return Response()
        assert (method, url) == ("GET", yt.CHANNELS_URL)
        return Response(data={"items": [{"id": "channel-1", "snippet": {"title": "Canal GTA"}}]})

    monkeypatch.setattr(yt, "youtube_request", google)
    callback = {"state": query["state"], "code": ["code"]}
    publisher.callback(callback)
    assert publisher.state()["channel"] == {"id": "channel-1", "title": "Canal GTA"}
    assert "token" not in json.dumps(publisher.state())
    with pytest.raises(ValueError, match="inválida"):
        publisher.callback(callback)
    publisher.disconnect()
    assert publisher.state()["channel"] is None


def test_short_uses_rotated_dimensions_and_schedules_in_utc(tmp_path, monkeypatch):
    def probe(*args, **kwargs):
        return yt.subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": [{"width": 1920,
            "height": 1080, "side_data_list": [{"rotation": -90}]}], "format": {"duration": "180"}}))

    monkeypatch.setattr(yt.subprocess, "run", probe)
    monkeypatch.setattr(yt.common, "tool", lambda name: name)
    body = {"title": "Corte", "kind": "short", "made_for_kids": False, "visibility": "private",
            "publish_at": "2090-02-03T13:00:00-03:00"}
    assert yt.media_metadata(body, tmp_path / "test.mp4")["status"]["publishAt"] == "2090-02-03T16:00:00+00:00"
    with pytest.raises(ValueError, match="classifica"):
        yt.media_metadata({**body, "kind": "video"}, tmp_path / "test.mp4")
    with pytest.raises(ValueError, match="Privado"):
        yt.media_metadata({**body, "visibility": "public"}, tmp_path / "test.mp4")


def test_resume_survives_server_restart_and_does_not_make_another_insert(tmp_path, monkeypatch):
    class InlineThread:
        # Run after start returns so the synchronous test never holds the publisher lock.
        tasks = []

        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.tasks.append((self.target, self.args))

    monkeypatch.setattr(yt.threading, "Thread", InlineThread)
    jobs = tmp_path / "jobs"
    state = tmp_path / "state"
    publisher = yt.YouTubePublisher(state, jobs)
    path = publisher.inbox(create=True) / "clip.mp4"
    path.write_bytes(b"abcde")
    account = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh", "channel": {"id": "c", "title": "Canal"}}
    publisher._write("youtube_account.json", account)
    monkeypatch.setattr(yt, "CHUNK_SIZE", 3)
    monkeypatch.setattr(yt, "media_metadata", lambda body, path: {"snippet": {"title": body["title"]}, "status": {}})
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret-upload-id"
    attempts = []

    def google(method, url, **kwargs):
        if url == yt.TOKEN_URL:
            return Response(data={"access_token": "access"})
        if url == yt.UPLOAD_URL:
            attempts.append("insert")
            return Response(Location=session)
        assert (method, url) == ("PUT", session)
        content_range = kwargs["headers"]["Content-Range"]
        attempts.append(content_range)
        if content_range in ("bytes 0-2/5", "bytes */5"):
            return Response(308, Range="bytes=0-2")
        assert content_range == "bytes 3-4/5"
        assert kwargs["data"] == b"de"
        if attempts.count(content_range) == 1:
            raise ValueError("Falha de conexão simulada")
        return Response(data={"id": "aBcDeFgHiJ1", "status": {"privacyStatus": "private"}})

    monkeypatch.setattr(yt, "youtube_request", google)
    body = {"confirm": True, "channel_id": "c", "file": "youtube/clip.mp4", "kind": "short", "title": "Corte"}
    with pytest.raises(ValueError, match="Confirme"):
        publisher.start({**body, "confirm": False})
    upload_id = publisher.start(body)
    target, args = InlineThread.tasks.pop()
    target(*args)
    assert publisher.state()["uploads"][0]["progress"] == 60
    assert "secret-upload-id" not in json.dumps(publisher.state())

    restarted = yt.YouTubePublisher(state, jobs)
    assert restarted.start({"confirm": True, "channel_id": "c", "retry_id": upload_id}) == upload_id
    target, args = InlineThread.tasks.pop()
    target(*args)
    row = restarted.state()["uploads"][0]
    assert (row["status"], row["video_id"], row["visibility"]) == ("sent", "aBcDeFgHiJ1", "private")
    assert attempts == ["insert", "bytes 0-2/5", "bytes 3-4/5", "bytes */5", "bytes 3-4/5"]
    with pytest.raises(ValueError, match="já tem um envio"):
        restarted.start(body)


@pytest.mark.parametrize("completed", [False, True])
def test_expired_schedule_never_sends_remaining_bytes(tmp_path, monkeypatch, completed):
    publisher = yt.YouTubePublisher(tmp_path / "state", tmp_path / "jobs")
    path = publisher.inbox(create=True) / "clip.mp4"
    path.write_bytes(b"abc")
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=first"
    account = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    row = {"id": "upload", "size": 3, "mtime_ns": path.stat().st_mtime_ns, "session": session, "status": "uploading",
           "metadata": {"snippet": {"title": "Corte"}, "status": {"publishAt": "2020-01-01T00:00:00+00:00"}}}
    publisher._write("youtube_uploads.json", [row])

    def google(method, url, **kwargs):
        if url == yt.TOKEN_URL:
            return Response(data={"access_token": "access"})
        assert (method, url) == ("PUT", session)
        assert kwargs["headers"]["Content-Range"] == "bytes */3"
        assert kwargs["data"] == b""
        if completed:
            return Response(data={"id": "aBcDeFgHiJ1", "status": {"privacyStatus": "public"}})
        return Response(308, Range="bytes=0-1")

    monkeypatch.setattr(yt, "youtube_request", google)
    publisher._worker(row, account, path)
    saved = publisher._history()[0]
    assert saved["status"] == ("sent" if completed else "schedule_expired")
    assert saved["session"] is None
