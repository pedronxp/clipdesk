"""Local OAuth connection and resumable YouTube uploads for the dashboard."""

import base64
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests

import common


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly")
CHUNK_SIZE = 8 * 1024 * 1024
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm"}
VISIBILITY = {"private", "unlisted", "public"}


def youtube_request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=(10, 90), allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise ValueError("Falha de conexão com o Google. Tente novamente.") from None


def response_data(response, message):
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not 200 <= response.status_code < 300:
        error = data.get("error")
        if isinstance(error, dict):
            errors = error.get("errors") or [{}]
            error = errors[0].get("reason")
        reason = error if isinstance(error, str) and re.fullmatch(r"[a-zA-Z_]{1,64}", error) else response.status_code
        raise ValueError(f"{message} ({reason}).")
    return data


def media_metadata(body, path):
    title = str(body.get("title") or "").strip()
    description = str(body.get("description") or "").strip()
    visibility = body.get("visibility")
    kind = body.get("kind")
    if not title or len(title) > 100 or len(description.encode("utf-8")) > 5000 or any(c in title + description for c in "<>"):
        raise ValueError("Use um título de até 100 caracteres e uma descrição de até 5.000 bytes. Remova os sinais < e >.")
    if not isinstance(visibility, str) or visibility not in VISIBILITY or kind not in ("short", "video") or type(body.get("made_for_kids")) is not bool:
        raise ValueError("Escolha o formato, a visibilidade e se o conteúdo é para crianças.")
    try:
        result = subprocess.run([common.tool("ffprobe"), "-v", "error", "-select_streams", "V:0",
                                 "-show_entries", "format=duration:stream=width,height:stream_side_data=rotation",
                                 "-of", "json", str(path)], check=True, capture_output=True, timeout=20)
        probe = json.loads(result.stdout)
        stream = probe["streams"][0]
        duration = float(probe["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        rotation = next((int(side["rotation"]) for side in stream.get("side_data_list", []) if "rotation" in side), 0)
        if rotation % 180 == 90:
            width, height = height, width
        if not math.isfinite(duration) or min(duration, width, height) <= 0:
            raise ValueError
    except (OSError, KeyError, IndexError, ValueError, TypeError, subprocess.SubprocessError, SystemExit):
        raise ValueError("Não foi possível verificar o formato e a duração do vídeo com o FFprobe.") from None
    short = width <= height and duration <= 180
    if kind == "short" and not short:
        raise ValueError("Para enviar um Short, use um vídeo vertical ou quadrado de até 3 minutos.")
    if kind == "video" and short:
        raise ValueError("O YouTube classifica esse arquivo como Short. Selecione Short ou use um vídeo horizontal ou acima de 3 minutos.")
    status = {"privacyStatus": visibility, "selfDeclaredMadeForKids": body["made_for_kids"]}
    if body.get("publish_at"):
        try:
            publish_at = datetime.fromisoformat(body["publish_at"])
            if publish_at.tzinfo is None or publish_at <= datetime.now(timezone.utc) or visibility != "private":
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Para agendar, selecione Privado e informe uma data e hora futuras.") from None
        status["publishAt"] = publish_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"snippet": {"title": title, "description": description, "categoryId": "20"}, "status": status}


class ScheduleExpired(ValueError):
    pass


class YouTubePublisher:
    def __init__(self, state_dir, jobs):
        self.directory = state_dir
        self.jobs = jobs
        self.lock = threading.Lock()
        self.pending = None
        self.connecting = None
        self.active = None

    def _read(self, name, default):
        try:
            return common.load_json(self.directory / name)
        except FileNotFoundError:
            return default

    def _write(self, name, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        temporary = path.with_suffix(".tmp")
        if os.name != "nt":
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        common.save_json(temporary, value)
        temporary.replace(path)

    def _account(self):
        return self._read("youtube_account.json", {})

    def _history(self):
        return self._read("youtube_uploads.json", [])

    def inbox(self, *, create=False):
        folder = self.jobs.resolve() / "youtube"
        if folder.resolve() != folder:
            raise ValueError("A pasta local de vídeos está indisponível.")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    def state(self):
        with self.lock:
            rows = self._history()
            visible = []
            for row in reversed(rows[-30:]):
                item = {k: row[k] for k in ("id", "channel", "kind", "status", "progress", "error", "video_id", "visibility", "publish_at") if k in row}
                item["title"] = row["metadata"]["snippet"]["title"]
                if row["status"] == "uploading" and row["id"] != self.active:
                    item.update(status="failed", error="Envio interrompido. Retome o envio pelo histórico.")
                visible.append(item)
            folder = self.inbox()
            files = sorted(p.name for p in folder.iterdir() if p.suffix.lower() in VIDEO_TYPES and p.is_file()) if folder.is_dir() else []
            return {"channel": self._account().get("channel"), "busy": self.active is not None, "uploads": visible, "files": files}

    def authorize(self, body, redirect_uri):
        credentials = body.get("credentials")
        config = credentials.get("installed") if isinstance(credentials, dict) else None
        client_id = config.get("client_id") if isinstance(config, dict) else None
        secret = config.get("client_secret") if client_id else None
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id) or not isinstance(secret, str) or not secret:
            raise ValueError("Selecione o JSON de um cliente OAuth do tipo Aplicativo para computador.")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        client = {"client_id": client_id, "client_secret": secret}
        with self.lock:
            if self.active:
                raise ValueError("Aguarde o envio terminar para conectar outro canal.")
            self.connecting = None
            self.pending = (state, verifier, client, redirect_uri, time.monotonic() + 600)
        return AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(SCOPES), "state": state, "access_type": "offline",
            "prompt": "consent select_account", "code_challenge": challenge, "code_challenge_method": "S256"})

    def callback(self, query):
        state = query.get("state", [""])[0]
        with self.lock:
            pending = self.pending
            if not pending or not secrets.compare_digest(state.encode(), pending[0].encode()) or time.monotonic() > pending[-1]:
                raise ValueError("Autorização expirada ou inválida. Conecte o canal novamente.")
            self.pending = None
            self.connecting = state
        if "error" in query or not query.get("code"):
            raise ValueError("Autorização cancelada no Google.")
        _, verifier, client, redirect_uri, _ = pending
        response = youtube_request("POST", TOKEN_URL, data={**client, "grant_type": "authorization_code",
            "code": query["code"][0], "code_verifier": verifier, "redirect_uri": redirect_uri})
        tokens = response_data(response, "O Google não autorizou a conexão")
        scope = tokens.get("scope", " ".join(SCOPES))
        if not tokens.get("refresh_token") or not tokens.get("access_token") or not set(SCOPES).issubset(scope.split()):
            raise ValueError("Autorize as permissões para identificar o canal e enviar vídeos.")
        response = youtube_request("GET", CHANNELS_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"},
                                   params={"part": "snippet", "mine": "true", "fields": "items(id,snippet/title),nextPageToken"})
        result = response_data(response, "Não foi possível identificar o canal")
        channels = result.get("items") or []
        if result.get("nextPageToken") or len(channels) != 1 or not channels[0].get("id"):
            raise ValueError("Selecione uma conta associada a um único canal do YouTube.")
        channel = {"id": channels[0]["id"], "title": channels[0]["snippet"]["title"]}
        with self.lock:
            if self.connecting != state or self.active:
                raise ValueError("A conexão foi substituída. Abra a aba do YouTube e confira o canal.")
            self.connecting = None
            self._write("youtube_account.json", {**client, "refresh_token": tokens["refresh_token"], "channel": channel})

    def disconnect(self):
        with self.lock:
            if self.active:
                raise ValueError("Aguarde o envio terminar para desconectar o canal.")
            self.pending = None
            self.connecting = None
            account = self._account()
            if account:
                response = youtube_request("POST", REVOKE_URL, data={"token": account["refresh_token"]})
                if response.status_code != 200:
                    try:
                        response_data(response, "Não foi possível revogar o acesso no Google")
                    except ValueError as error:
                        try:
                            already_revoked = response.status_code == 400 and response.json().get("error") == "invalid_token"
                        except (AttributeError, ValueError):
                            already_revoked = False
                        if not already_revoked:
                            raise error from None
            (self.directory / "youtube_account.json").unlink(missing_ok=True)

    def _file(self, relative):
        path = (self.jobs / relative).resolve()
        if not path.is_relative_to(self.jobs.resolve()) or path.suffix.lower() not in VIDEO_TYPES or not path.is_file():
            raise ValueError("Escolha um vídeo disponível na pasta jobs/.")
        return path

    def start(self, body):
        if body.get("confirm") is not True:
            raise ValueError("Confirme o envio ao YouTube.")
        retry_id = body.get("retry_id")
        if not retry_id:
            path = self._file(str(body.get("file") or ""))
            stat = path.stat()
            metadata = media_metadata(body, path)
        with self.lock:
            if self.active:
                raise ValueError("Já há um envio ao YouTube em andamento.")
            account = self._account()
            channel = account.get("channel")
            if not channel or body.get("channel_id") != channel["id"]:
                raise ValueError("Confirme o canal conectado antes de enviar.")
            rows = self._history()
            if retry_id:
                row = next((r for r in rows if r["id"] == retry_id and r["channel"]["id"] == channel["id"]), None)
                if not row or row["status"] not in ("failed", "uploading"):
                    raise ValueError("Este envio não pode ser retomado.")
                path = self._file(row["file"])
            else:
                relative = path.relative_to(self.jobs.resolve()).as_posix()
                if any(r["status"] != "schedule_expired" and r["channel"]["id"] == channel["id"] and r["file"] == relative and
                       r["mtime_ns"] == stat.st_mtime_ns and r["size"] == stat.st_size for r in rows):
                    raise ValueError("Este arquivo já tem um envio para o canal. Consulte o histórico.")
                row = {"id": secrets.token_hex(12), "file": relative, "size": stat.st_size,
                       "mtime_ns": stat.st_mtime_ns, "channel": channel, "kind": body["kind"],
                       "metadata": metadata, "progress": 0}
                rows.append(row)
            self._check_file(row, path.stat())
            row.update(status="uploading", error=None)
            self._write("youtube_uploads.json", rows)
            self.active = row["id"]
            threading.Thread(target=self._worker, args=(row, account, path), daemon=True).start()
            return row["id"]

    @staticmethod
    def _check_file(row, stat):
        if not 0 < stat.st_size <= 256 * 1024**3:
            raise ValueError("O arquivo deve ter entre 1 byte e 256 GB.")
        if (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime_ns"]):
            raise ValueError("O arquivo foi alterado desde o início do envio.")

    def _save_upload(self, row, **changes):
        with self.lock:
            rows = self._history()
            row.update(changes)
            self._write("youtube_uploads.json", [row if r["id"] == row["id"] else r for r in rows])

    def _worker(self, row, account, path):
        try:
            result = self._upload(row, account, path)
            video_id = result.get("id")
            if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                raise ValueError("O YouTube não retornou o ID do vídeo. Verifique o YouTube Studio antes de retomar.")
            status = result.get("status") or {}
            self._save_upload(row, status="sent", progress=100, video_id=video_id,
                              visibility=status.get("privacyStatus"), publish_at=status.get("publishAt"), session=None)
        except ScheduleExpired as error:
            self._save_upload(row, status="schedule_expired", error=str(error), session=None)
        except Exception as error:
            self._save_upload(row, status="failed", error=str(error) if isinstance(error, ValueError) else "Falha no envio. Verifique o arquivo e tente retomar.")
        finally:
            with self.lock:
                self.active = None

    def _upload(self, row, account, path):
        token = None
        expires = 0

        def auth_headers():
            nonlocal token, expires
            if time.monotonic() >= expires:
                response = youtube_request("POST", TOKEN_URL, data={"client_id": account["client_id"],
                    "client_secret": account["client_secret"], "refresh_token": account["refresh_token"], "grant_type": "refresh_token"})
                data = response_data(response, "O Google recusou a sessão. Reconecte o canal")
                token = data["access_token"]
                expires = time.monotonic() + max(1, int(data.get("expires_in", 3600)) - 120)
            return {"Authorization": f"Bearer {token}"}

        size = row["size"]
        mime = VIDEO_TYPES[path.suffix.lower()]
        session = row.get("session")
        if not session:
            self._check_schedule(row)
            response = youtube_request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                headers={**auth_headers(), "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime}, json=row["metadata"])
            response_data(response, "O YouTube recusou o início do envio")
            session = response.headers.get("Location", "")
            location = urlsplit(session)
            if location.scheme != "https" or location.netloc not in {"www.googleapis.com", "youtube.googleapis.com"}:
                raise ValueError("O YouTube não retornou uma sessão de envio válida.")
            self._save_upload(row, session=session)
            offset = 0
        else:
            response = youtube_request("PUT", session, headers={**auth_headers(), "Content-Range": f"bytes */{size}"}, data=b"")
            if response.status_code != 308:
                return response_data(response, "Não foi possível retomar a sessão. Confira este vídeo no YouTube Studio")
            offset = self._offset(response, size)

        with path.open("rb") as media:
            while offset < size:
                self._check_file(row, os.fstat(media.fileno()))
                media.seek(offset)
                chunk = media.read(min(CHUNK_SIZE, size - offset))
                if not chunk:
                    raise ValueError("O arquivo foi alterado durante o envio.")
                end = offset + len(chunk) - 1
                headers = auth_headers()
                self._check_schedule(row)
                response = youtube_request("PUT", session, headers={**headers, "Content-Type": mime,
                    "Content-Range": f"bytes {offset}-{end}/{size}"}, data=chunk)
                if response.status_code != 308:
                    return response_data(response, "O YouTube recusou o envio")
                next_offset = self._offset(response, size)
                if not offset < next_offset <= end + 1:
                    raise ValueError("O YouTube não confirmou o trecho enviado. Retome pelo histórico.")
                offset = next_offset
                self._save_upload(row, progress=offset * 100 // size)
        raise ValueError("Confirmação final pendente. Retome pelo histórico.")

    @staticmethod
    def _check_schedule(row):
        publish_at = row["metadata"]["status"].get("publishAt")
        if publish_at and datetime.fromisoformat(publish_at) <= datetime.now(timezone.utc):
            raise ScheduleExpired("O horário agendado passou. Selecione o mesmo arquivo e informe um novo horário.")

    @staticmethod
    def _offset(response, size):
        received = response.headers.get("Range")
        if not received:
            return 0
        match = re.fullmatch(r"bytes=0-(\d+)", received)
        if not match or int(match[1]) >= size:
            raise ValueError("O YouTube retornou uma posição de envio inválida.")
        return int(match[1]) + 1
