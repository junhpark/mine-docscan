"""로컬 검수 화면. 표준 라이브러리(http.server)와 한 장짜리 HTML 만 쓴다 — 현장은 외부 네트워크가 없다 (ADR 0003).

  GET  /                                   화면 (static/index.html)
  GET  /api/queue?name=&n=&seed=&empty_share=&template=&kind=   항목 목록과 진행 수
  GET  /crop?field_id=&kind=cell|row|pair[&scale=][&box=0]      PNG. 응답 머리글 X-Crop-Source: source | aligned
                                           (pair: 점검표 행의 유·무 두 칸을 같이, box=0: 행 띠에 대상 칸 테두리 없이)
  POST /api/review  {field_id, verdict, value, note}            저장 (검수자는 서버를 띄울 때 정한다)
  POST /api/reviews {items: [{field_id, verdict, value, note}]}  한 항목의 칸 여럿을 한 번에 — 전부 검사한 뒤에야 저장한다
                                           (한 칸이라도 형식에 맞지 않으면 아무것도 남지 않는다, tasks/0005 4.1)
  POST /api/check   {field_id, answer: yes|no|none|unknown}     ✓ 행의 답 → 두 칸의 검수 두 건 (review/checks.py)
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
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..forms.formats import FORMATS, HINTS, INPUT_CHARS, FormatError, normalize
from .crops import CropError, cell_crop, field_info, pair_crop, row_crop
from .queue import INPUT_KINDS, QUEUES, build_queue
from .store import VERDICTS, field_format, review_from_field, save, stats

STATIC = Path(__file__).parent / "static"
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
        for k, cast in (("n", int), ("seed", int), ("empty_share", float), ("template", str), ("kind", str), ("audit", int)):
            if params.get(k):
                try:
                    opts[k] = cast(params[k])
                except ValueError as e:
                    raise ApiError(400, f"{k} 값이 잘못되었습니다: {params[k]!r}") from e
        name = params.get("name") or self.queue
        if name not in QUEUES:
            raise ApiError(400, f"알 수 없는 대기열: {name}")
        q = build_queue(self.con, name, site=self.site,
                        **{k: v for k, v in opts.items() if k in ("n", "seed", "empty_share", "template", "kind", "audit")})
        q.update(reviewer=self.reviewer, site=self.site.name, show_machine=(name == "pending"),
                 templates={t.name: t.title for t in self.site.templates.values()},
                 formats={f: {"chars": INPUT_CHARS[f], "hint": HINTS[f]} for f in FORMATS})
        return q

    def crop_png(self, params: dict) -> tuple[bytes, str]:
        """(PNG, 출처). 출처는 source(원본 해상도) 또는 aligned(정합 이미지)."""
        fid, kind = params.get("field_id", ""), params.get("kind", "cell")
        try:
            if kind == "row":
                return row_crop(self.con, self.settings, fid, box=params.get("box") != "0")
            if kind == "pair":
                from .checks import row_of

                row = row_of(self.con, self.site, fid)
                if row is None:
                    raise ApiError(400, "점검표 장비 행의 체크 칸이 아닙니다")
                return pair_crop(self.con, self.settings, row.yes["field_id"], row.no["field_id"])
            if kind == "cell":
                scale = _int_param(params, "scale", 3)
                if not 1 <= scale <= 6:
                    raise ApiError(400, f"scale 은 1–6: {scale}")
                return cell_crop(self.con, self.settings, fid, scale=scale)
        except KeyError as e:
            raise ApiError(404, f"없는 필드: {fid}") from e
        except CropError as e:
            raise ApiError(409, str(e)) from e
        raise ApiError(400, f"kind 는 cell, row, pair 중 하나: {kind}")

    def post_review(self, body: dict) -> dict:
        review = self._checked_review(body)
        try:
            out = save(self.con, self.site, self.settings, review)
        except FormatError as e:
            raise ApiError(400, str(e)) from e
        return {"ok": True, **out, "field_id": review.field_id, "verdict": review.verdict, "value": review.value,
                "reviewed_at": review.reviewed_at}

    def post_reviews(self, body: dict) -> dict:
        """한 항목의 칸 여럿 (계기의 시작·종료·총, 묶음 항목). 전부 검사하고 정규화한 뒤에야 저장한다 — 한 칸이라도 틀리면 400 이고
        검수 파일에 아무것도 남지 않는다. 같은 칸이 두 번이면 거절. 저장 시각은 하나."""
        from .store import now_iso

        if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not body["items"]:
            raise ApiError(400, "본문은 {items: [...]} 이어야 합니다")
        reviews = []
        for k, item in enumerate(body["items"]):
            try:
                reviews.append(self._checked_review(item))
            except ApiError as e:
                raise ApiError(e.status, f"{k + 1}번째 칸: {e}") from e
        if len({r.field_id for r in reviews}) != len(reviews):
            raise ApiError(400, "같은 칸이 두 번 들어 있습니다")
        at = now_iso()
        saved = []
        for r in (replace(r, reviewed_at=at, review_id="") for r in reviews):     # review_id 는 저장 시각에서 다시 만든다
            try:
                out = save(self.con, self.site, self.settings, r)
            except FormatError as e:                                   # 위에서 검사했으므로 오지 않는다 — 와도 500 이 아니라 400
                raise ApiError(400, str(e)) from e
            saved.append({"field_id": r.field_id, "verdict": r.verdict, "value": r.value, "review_id": out["review_id"],
                          "applied": out["applied"]})
        return {"ok": True, "saved": saved, "reviewed_at": at}

    def _checked_review(self, body: dict):
        """요청 하나 → 검사하고 정규화한 Review (아직 저장하지 않는다)."""
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
            # 칸의 형식(forms/formats.py)으로 정규화 — "07" → "7", "8-17" → "08:00~17:00". 정답 파일과 인식기 출력이 같은 표기여야
            # 채점이 맞는다. 맞지 않으면 400 이고 검수 파일에 아무것도 남지 않는다 (store.save 도 다시 검사한다)
            try:
                value = normalize(field_format(self.site, f["template_name"], f["region"], f["field_name"]), value)
            except FormatError as e:
                raise ApiError(400, str(e)) from e
        return review_from_field(self.con, fid, verdict, value, self.reviewer, note=note)

    def post_check(self, body: dict) -> dict:
        """✓ 행의 답 하나 → 유·무 두 칸의 검수 두 건 (tasks/0004 4.9). 둘 다 저장한 뒤 insp_daily 가 그 답을 따른다."""
        from .checks import ANSWERS, check_reviews, row_of

        if not isinstance(body, dict):
            raise ApiError(400, "본문은 JSON 객체여야 합니다")
        fid, answer = str(body.get("field_id", "")), str(body.get("answer", ""))
        if answer not in ANSWERS:
            raise ApiError(400, f"answer 는 {tuple(ANSWERS)} 중 하나: {answer}")
        row = row_of(self.con, self.site, fid)
        if row is None:
            raise ApiError(404 if field_info(self.con, fid) is None else 400, f"점검표 장비 행의 체크 칸이 아닙니다: {fid}")
        reviews = check_reviews(self.con, row, ANSWERS[answer], self.reviewer, note=str(body.get("note", "") or ""))
        outs = [save(self.con, self.site, self.settings, rv) for rv in reviews]
        return {"ok": True, "item_id": row.item_id, "answer": ANSWERS[answer], "review_ids": [o["review_id"] for o in outs],
                "applied": all(o["applied"] for o in outs), "reviewed_at": reviews[0].reviewed_at}

    def stats_json(self) -> dict:
        return stats(self.con, self.site)


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
        except (BrokenPipeError, ConnectionResetError):        # 화면이 응답을 기다리지 않고 넘어갔다 (빠르게 다음 항목으로)
            return
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:                                 # noqa: BLE001 — 연결을 끊지 않고 500 으로 답한다
            self._json(500, {"error": f"서버 오류: {type(e).__name__}"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path not in ("/api/review", "/api/reviews", "/api/check"):
                raise ApiError(404, "없는 경로")
            self._check_local(need_json=True)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise ApiError(400, "본문이 JSON 이 아닙니다") from e
            post = {"/api/review": self.app.post_review, "/api/reviews": self.app.post_reviews, "/api/check": self.app.post_check}
            self._json(200, post[u.path](body))
        except (BrokenPipeError, ConnectionResetError):
            return
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
