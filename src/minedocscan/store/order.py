"""문서·쪽의 순서 (tasks/0007 4.6, 4.8) — 같은 내용의 DB 가 같은 결과를 내도록, 행이 들어간 순서 대신 이것으로 정렬한다.

문서의 순서 = (접수한 문서인가 — archive_root 의 intake/ 아래면 뒤, 보관 경로(source_rel, 없으면 source_path)를 / 로 나눈 성분들,
document_id). 성분은 코드포인트로 견준다 — 파이썬의 글자열 비교. SQL 의 ORDER BY 에 맡기지 않는다 (DB 마다 글자 순서가 다르다).
접수한 문서의 보관 폴더 이름은 받은 시각으로 시작하므로(intake/<해-달>/<받은 시각>-<문서 ID>/) 받은 순서와 같고, DB 를 지우고
다시 만들어도 같다. 묶음 폴더는 지금 `run` 이 한 폴더를 도는 순서(Pipeline.expand — 경로의 성분별 정렬)와 같다.
쪽의 순서 = (문서의 순서, 쪽 번호).
"""
from __future__ import annotations

import hashlib
from pathlib import PurePath

INTAKE_DIR = "intake"        # archive_root 아래 접수한 문서의 보관 폴더 (intake/inbox.py)


def document_key(source_rel: str | None, source_path: str | None, document_id: str) -> tuple:
    """문서의 순서 키."""
    if source_rel:
        parts = tuple(p for p in str(source_rel).replace("\\", "/").split("/") if p)
    else:
        parts = PurePath(str(source_path or "")).as_posix().split("/")
        parts = tuple(p for p in parts if p)
    intake = bool(source_rel) and parts[:1] == (INTAKE_DIR,)
    return (int(intake), parts, document_id)


def row_document_key(row) -> tuple:
    """doc_document 행(또는 source_rel·source_path·document_id 가 있는 사전)의 순서 키."""
    return document_key(row["source_rel"], row["source_path"], row["document_id"])


def page_key(source_rel: str | None, source_path: str | None, document_id: str, page_no: int) -> tuple:
    return (document_key(source_rel, source_path, document_id), int(page_no))


def document_id(data: bytes) -> str:
    """문서 ID = 파일 바이트의 SHA-256 앞 16자리 — 같은 스캔은 한 문서 (등록·접수·합성의 truth 가 같이 쓴다)."""
    return hashlib.sha256(data).hexdigest()[:16]
