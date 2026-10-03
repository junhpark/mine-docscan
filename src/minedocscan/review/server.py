"""로컬 검수 화면. 표준 라이브러리(http.server)와 한 장짜리 HTML 만 쓴다 — 현장은 외부 네트워크가 없다 (ADR 0003).

  GET  /                                   화면 (static/index.html)
  GET  /api/queue?name=&n=&seed=&empty_share=&template=&kind=   항목 목록과 진행 수
  GET  /crop?field_id=&kind=cell|row[&scale=]                   PNG. 응답 머리글 X-Crop-Source: source | aligned
  POST /api/review  {field_id, verdict, value, note}            저장 (검수자는 서버를 띄울 때 정한다)
  GET  /api/stats                          진행 현황

127.0.0.1 에만 바인딩한다 — 화면에 실제 이름과 차량번호가 보인다. 단일 스레드(SQLite 연결 하나).
서버 로그에는 요청 경로와 상태 코드만 찍는다. 입력값과 이미지 내용은 찍지 않는다.
POST 는 같은 브라우저에 열린 다른 페이지가 보낼 수 없게 Host·Origin(로컬 주소만)과 Content-Type(JSON)을 확인한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .crops import CropError, cell_crop, field_info, row_crop
from .queue import INPUT_KINDS, QUEUES, build_queue
from .store import VERDICTS, review_from_field, save, stats

STATIC = Path(__file__).parent / "static"
DIGITS = re.compile(r"^[0-9]+$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params[key]) if params.get(key) else default
    except ValueError as e:
        raise ApiError(400, f"{key} 는 정수여야 합니다: {params[key]!r}") from e


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ReviewApp:
    """HTTP 와 무관한 처리부. 테스트에서는 서버 없이 바로 부를 수 있다."""

    def __init__(self, con: sqlite3.Connection, site, settings, reviewer: str, queue: str = "haul-numbers",
                 queue_opts: dict | None = None):
        if not reviewer or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", reviewer):
            raise ValueError("검수자 이름은 짧은 영문·숫자 식별자여야 합니다 (--reviewer)")
        if queue not in QUEUES:
            raise ValueError(f"알 수 없는 대기열 '{queue}' (가능: {QUEUES})")
        self.con, self.site, self.settings, self.reviewer = con, site, settings, reviewer
        self.queue, self.queue_opts = queue, dict(queue_opts or {})

    def queue_json(self, params: dict) -> dict:
        opts = dict(self.queue_opts)
        for k, cast in (("n", int), ("seed", int), ("empty_share", float), ("template", str), ("kind", str)):
            if params.get(k):
                try:
                    opts[k] = cast(params[k])
                except ValueError as e:
                    raise ApiError(400, f"{k} 값이 잘못되었습니다: {params[k]!r}") from e
        name = params.get("name") or self.queue
        if name not in QUEUES:
            raise ApiError(400, f"알 수 없는 대기열: {name}")
        q = build_queue(self.con, name, site=self.site,
                        **{k: v for k, v in opts.items() if k in ("n", "seed", "empty_share", "template", "kind")})
        q.update(reviewer=self.reviewer, site=self.site.name, show_machine=(name == "pending"),
                 templates={t.name: t.title for t in self.site.templates.values()})
        return q

    def crop_png(self, params: dict) -> tuple[bytes, str]:
        """(PNG, 출처). 출처는 source(원본 해상도) 또는 aligned(정합 이미지)."""
        fid, kind = params.get("field_id", ""), params.get("kind", "cell")
        try:
            if kind == "row":
                return row_crop(self.con, self.settings, fid)
            if kind == "cell":
                scale = _int_param(params, "scale", 3)
                if not 1 <= scale <= 6:
                    raise ApiError(400, f"scale 은 1–6: {scale}")
                return cell_crop(self.con, self.settings, fid, scale=scale)
        except KeyError as e:
            raise ApiError(404, f"없는 필드: {fid}") from e
        except CropError as e:
            raise ApiError(409, str(e)) from e
        raise ApiError(400, f"kind 는 cell 또는 row: {kind}")

    def post_review(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "본문은 JSON 객체여야 합니다")
        fid, verdict = str(body.get("field_id", "")), str(body.get("verdict", ""))
        value, note = str(body.get("value", "")).strip(), str(body.get("note", "") or "")
        f = field_info(self.con, fid)
        if f is None:
            raise ApiError(404, f"없는 필드: {fid}")
        if f["kind"] not in INPUT_KINDS:
            raise ApiError(400, f"이 화면에서 입력할 수 없는 셀 종류: {f['kind']}")
        if verdict not in VERDICTS:
            raise ApiError(400, f"판정은 {VERDICTS} 중 하나: {verdict}")
        if verdict == "value":
            if not value:
                raise ApiError(400, "값이 비었습니다 (빈 칸이면 verdict=empty)")
            if f["kind"] == "handwritten_number":
                if not DIGITS.match(value):
                    raise ApiError(400, f"숫자 셀에는 숫자만: {value!r}")
                value = str(int(value))              # "07" → "7": 정답 파일과 인식기 출력이 같은 표기여야 채점이 맞는다
        review = review_from_field(self.con, fid, verdict, value, self.reviewer, note=note)
        out = save(self.con, self.site, self.settings, review)
        return {"ok": True, **out, "field_id": fid, "verdict": verdict, "value": review.value,
                "reviewed_at": review.reviewed_at}

    def stats_json(self) -> dict:
        return stats(self.con)


def index_html() -> bytes:
    return (STATIC / "index.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    app: ReviewApp
    server_version = "minedocscan-review"

    def log_request(self, code="-", size="-"):             # 경로와 상태 코드만. 질의·본문은 찍지 않는다
        print(f"{self.command} {self.path.split('?', 1)[0]} {code}", file=sys.stderr)

    def log_error(self, fmt, *args):
        return None

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _check_local(self, need_json: bool) -> None:
        """로컬 화면이 보낸 요청인지: Host 와 (있으면) Origin 이 127.0.0.1/localhost, 쓰기는 JSON 본문만."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            raise ApiError(403, "로컬 주소로만 접근할 수 있습니다")
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            if o.scheme != "http" or (o.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
                raise ApiError(403, "다른 출처의 요청은 받지 않습니다")
        if need_json and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise ApiError(415, "Content-Type 은 application/json 이어야 합니다")

    def do_GET(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            self._check_local(need_json=False)
            if u.path == "/":
                self._send(200, index_html(), "text/html; charset=utf-8")
            elif u.path == "/api/queue":
                self._json(200, self.app.queue_json(params))
            elif u.path == "/api/stats":
                self._json(200, self.app.stats_json())
            elif u.path == "/crop":
                png, src = self.app.crop_png(params)
                self._send(200, png, "image/png", {"X-Crop-Source": src})
            elif u.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"error": "없는 경로"})
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:                                 # noqa: BLE001 — 연결을 끊지 않고 500 으로 답한다
            self._json(500, {"error": f"서버 오류: {type(e).__name__}"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path != "/api/review":
                raise ApiError(404, "없는 경로")
            self._check_local(need_json=True)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise ApiError(400, "본문이 JSON 이 아닙니다") from e
            self._json(200, self.app.post_review(body))
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:                                 # noqa: BLE001
            self._json(500, {"error": f"서버 오류: {type(e).__name__}"})


def make_server(app: ReviewApp, host: str = "127.0.0.1", port: int = 8765) -> HTTPServer:
    """서버를 만든다 (아직 돌리지는 않는다). port 0 이면 빈 포트를 고른다 — 테스트용."""
    handler = type("ReviewHandler", (_Handler,), {"app": app})
    try:
        return HTTPServer((host, port), handler)
    except OSError as e:
        raise OSError(f"포트 {port} 를 열 수 없습니다 ({e.strerror}). 다른 프로그램이 쓰고 있으면 --port 로 바꾸세요") from e


def serve(app: ReviewApp, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = make_server(app, host, port)
    print(f"검수 화면: http://{host}:{httpd.server_address[1]}/  (대기열 {app.queue}, 검수자 {app.reviewer}) — "
          f"Ctrl+C 로 끝냅니다", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
