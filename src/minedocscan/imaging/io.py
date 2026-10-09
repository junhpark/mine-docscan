"""이미지·PDF 입출력.

cv2.imread / cv2.imwrite 는 Windows 에서 한글 경로를 읽고 쓰지 못한다. 현장 파일명은 대부분 한글이므로
항상 여기의 함수를 쓴다 (np.fromfile + imdecode, imencode + tofile).

PDF 는 PDFium(pypdfium2 — Apache-2.0/BSD-3)으로 읽는다 (ADR 0023 — PyMuPDF 는 AGPL-3.0 이라 바꿨다). PDFium 은 여러 스레드에서 같이
쓰면 안 된다 (tasks/0007 4.9 — serve 의 작업 스레드는 쪽을 렌더링하고 화면 스레드는 크롭과 쪽 그림을 렌더링한다). PDFium 을 부르는
곳(열기·쪽 꺼내기·렌더링·닫기)은 전부 이 파일에 있고 PDF_LOCK 하나로 감싼다. 공개 함수가 잠금을 잡고, 밑줄로 시작하는 도우미는
잡힌 채로 불린다고 본다. load_pages 는 쪽을 내주는 동안(yield)에는 잠금을 놓는다. 쪽·문서는 잠금 안에서 직접 닫는다 (가비지 수집이
다른 스레드에서 닫지 않게). 합성 PDF 는 라이브러리 없이 쓴다 (tools/pdfwrite).
"""
from __future__ import annotations

import atexit
import math
import threading
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED_EXT = IMAGE_EXT | {".pdf"}
PDF_LOCK = threading.RLock()        # PDFium 을 부르는 곳 전부 (위의 설명)


def imread_gray(path: str | Path) -> np.ndarray:
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {path}")
    return img


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_size(path: str | Path) -> tuple[int, int]:
    """그림의 (너비, 높이). PNG 는 머리(IHDR)만 읽는다 — 사이트 팩을 읽을 때마다 큰 그림을 풀지 않게. 그 밖의 형식은 풀어서 잰다."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) == 24 and head[:8] == PNG_SIGNATURE and head[12:16] == b"IHDR":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    h, w = imread_gray(path).shape[:2]
    return w, h


def imwrite(path: str | Path, img: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise ValueError(f"이미지를 인코딩할 수 없습니다: {path}")
    buf.tofile(str(path))


class DamagedPdfError(ValueError):
    pass


DAMAGED_PDF_POLICIES = ("fail", "warn")
EOF_WINDOW = 1024                   # %%EOF 를 찾는 파일 끝의 바이트 수 (실제 PDF 30개 모두 마지막 1 KB 안에 있었다 — tasks/0009 1절 나)


def pdf_has_eof(data: bytes) -> bool:
    """파일의 마지막 1 KB 에 %%EOF 가 있나 — 전송 중 끊긴 PDF(50 %·90 %·99.9 % 로 자른 실제 묶음)는 셋 다 없었다."""
    return b"%%EOF" in data[-EOF_WINDOW:]


class _Pdf:
    """열린 PDF (pypdfium2). 부르는 쪽이 PDF_LOCK 을 잡고 쓰고 닫는다."""

    def __init__(self, doc, n: int, data: bytes = b""):
        self.doc, self.page_count = doc, n
        self._data = data                                # PDFium 이 읽는 메모리 — 닫을 때까지 놓지 않는다

    def render(self, index: int, dpi: int) -> np.ndarray:
        return _render_page(self.doc, index, dpi)

    def close(self) -> None:
        self.doc.close()
        self._data = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_EXIT_HOOK: list[bool] = []


def _pdfium():
    """pypdfium2 (처음 부를 때 끝낼 때의 고리를 하나 건다: pypdfium2 는 import 할 때 PDFium 을 닫는 atexit 를 걸고 atexit 는 거꾸로 돈다 —
    그보다 뒤에 건 우리 고리가 먼저 PDF_LOCK 을 잡아(놓지 않는다) serve 의 작업 스레드(데몬)가 쪽을 그리는 가운데 PDFium 이 닫히지 않게)."""
    import pypdfium2 as pdfium

    if not _EXIT_HOOK:
        atexit.register(PDF_LOCK.acquire, timeout=30)
        _EXIT_HOOK.append(True)
    return pdfium


def _open_pdf(path: Path, damaged: str = "fail", warnings: list[str] | None = None) -> _Pdf:
    """PDF 를 연다 (파일을 바이트로 읽어서 — 한글 경로를 라이브러리에 넘기지 않는다). 라이브러리의 오류 글은 싣지 않는다 (예외의 종류만).

    손상(tasks/0009 4.3 가): **열리지 않거나, 파일의 마지막 1 KB 에 %%EOF 가 없으면** 손상이다.
      fail  오류로 낸다 (기본) — 뒤쪽 쪽이 사라진 채 '양식 없음'으로 섞이면 손상을 알 수 없다
      warn  열 수 있으면 열고 warnings 에 한 줄 남긴다 (열 수 없으면 fail 과 같다) — 끝 표시를 빼먹는 스캐너를 위한 것
    쪽이 하나도 없는 파일은 어느 쪽이든 오류다.
    """
    pdfium = _pdfium()
    if damaged not in DAMAGED_PDF_POLICIES:
        raise ValueError(f"damaged_pdf 는 {DAMAGED_PDF_POLICIES} 중 하나: {damaged!r}")
    data = path.read_bytes()
    from pypdfium2 import raw as pdfium_c

    # 손잡이로 직접 연다 — pypdfium2 의 PdfDocument(bytes) 는 쪽이 0개인 파일을 "열 수 없다"로 거절하고 손잡이를 닫지 않는다
    handle = pdfium_c.FPDF_LoadMemDocument64(data, len(data), None)
    if not handle:
        raise DamagedPdfError(f"PDF 를 열 수 없습니다 (잘린 파일?): {path.name}. 원본을 다시 받으세요")
    n = pdfium_c.FPDF_GetPageCount(handle)
    if n < 1:
        pdfium_c.FPDF_CloseDocument(handle)
        raise DamagedPdfError(f"PDF 에 쪽이 없습니다: {path.name}")
    doc = pdfium.PdfDocument(handle)                     # 닫으면 손잡이를 닫는다. 바이트는 _Pdf 가 그때까지 들고 있다
    if not pdf_has_eof(data):
        if damaged != "warn":
            doc.close()
            raise DamagedPdfError(f"PDF 의 끝 표시(%%EOF)가 없습니다 (잘린 파일?): {path.name}. 원본을 다시 받으세요 "
                                  "(스캐너가 만든 멀쩡한 파일이면 [pipeline] damaged_pdf = \"warn\")")
        if warnings is not None:
            warnings.append(f"PDF 의 끝 표시(%%EOF)가 없지만 열어서 처리했습니다 (쪽 {n}개): {path.name}")
    return _Pdf(doc, n, data)


SIZE_FUZZ = 0.001                  # 그림 크기의 올림에서 봐주는 소수 — PyMuPDF(MuPDF 의 fz_round_rect)와 같다 (1000.001 → 1000, 1000.0011 → 1001)


def page_px(pt: float, dpi: int) -> int:
    """쪽의 길이(pt) → 화소: ceil(pt × dpi / 72 − 0.001) — PyMuPDF 와 같은 규칙 (재 보니 반올림이 아니라 0.001 을 봐주는 올림이다:
    1165.36 → 1166). pypdfium2 의 render(scale) 는 봐주지 않는 올림이라 합성 A4(1654.0000000000002)가 1655 px 이 된다 (tasks/0009 1절 나).
    실제 스캔의 그림 크기가 PyMuPDF 로 읽던 때와 같다."""
    return max(1, math.ceil(pt * dpi / 72 - SIZE_FUZZ))


def _render_page(doc, index: int, dpi: int) -> np.ndarray:
    """쪽 하나를 회색조로. 그림의 크기 = page_px (PyMuPDF 와 같은 규칙) — 그 크기의 비트맵에 PDFium 이 쪽을 맞춰 그린다."""
    import ctypes

    from pypdfium2 import raw as pdfium_c

    page = doc[index]
    try:
        w, h = page_px(page.get_width(), dpi), page_px(page.get_height(), dpi)
        buf = (ctypes.c_ubyte * (w * h))()
        bitmap = pdfium_c.FPDFBitmap_CreateEx(w, h, pdfium_c.FPDFBitmap_Gray, buf, w)
        try:
            pdfium_c.FPDFBitmap_FillRect(bitmap, 0, 0, w, h, 0xFFFFFFFF)
            pdfium_c.FPDF_RenderPageBitmap(bitmap, page, 0, 0, w, h, 0, pdfium_c.FPDF_ANNOT | pdfium_c.FPDF_GRAYSCALE)
        finally:
            pdfium_c.FPDFBitmap_Destroy(bitmap)
        return np.frombuffer(buf, np.uint8).reshape(h, w).copy()
    finally:
        page.close()


def load_pages(path: str | Path, dpi: int = 200, damaged: str = "fail",
               warnings: list[str] | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """파일 하나를 (페이지 번호, 회색조 이미지) 로 푼다. PDF 는 지정 dpi 로 렌더링한다.

    이미지 파일은 스캔 해상도 그대로 쓴다 — 정합 단계의 호모그래피가 배율 차이를 흡수한다.
    damaged·warnings: 손상 PDF 의 처리 (_open_pdf).
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        with PDF_LOCK:
            doc = _open_pdf(path, damaged, warnings)
            n = doc.page_count
        try:
            for i in range(1, n + 1):
                with PDF_LOCK:                                         # 쪽 하나를 렌더링하는 동안만 — 내주는 동안은 놓는다
                    img = doc.render(i - 1, dpi)
                yield i, img
        finally:
            with PDF_LOCK:
                doc.close()
    elif ext in IMAGE_EXT:
        yield 1, imread_gray(path)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {path}")


def count_pages(path: str | Path, damaged: str = "fail", warnings: list[str] | None = None) -> int:
    """파일을 한 번 열어 쪽 수를 센다 — 문서를 등록할 때 (tasks/0007 4.1). 열리지 않으면(쓰레기 바이트, 쪽이 없는 PDF, 손상 방침에
    걸린 PDF, 디코딩되지 않는 그림) 예외. 손상 방침이 warn 이면 warnings 에 한 줄."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        with PDF_LOCK, _open_pdf(path, damaged, warnings) as doc:
            return doc.page_count
    if ext in IMAGE_EXT:
        imread_gray(path)
        if ext in (".tif", ".tiff") and _tiff_frames(path) > 1:   # 둘째 쪽부터 말없이 빠지지 않게 (첫 쪽만 디코딩된다)
            raise ValueError("여러 쪽 TIFF 는 읽지 않습니다 — 스캐너에서 PDF 로 저장하세요")
        return 1
    raise ValueError(f"지원하지 않는 형식입니다: {path}")


def _tiff_frames(path: Path) -> int:
    ok, frames = cv2.imdecodemulti(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    return len(frames) if ok else 1


def load_page(path: str | Path, page_no: int, dpi: int = 200, damaged: str = "fail") -> np.ndarray:
    """한 쪽만 렌더링한다 (1부터). 원본 해상도 크롭처럼 쪽 하나가 필요할 때 — 앞쪽을 전부 렌더링하지 않는다."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        with PDF_LOCK, _open_pdf(path, damaged) as doc:
            if not 1 <= page_no <= doc.page_count:
                raise KeyError(f"{path} 에 {page_no}쪽이 없습니다 (전체 {doc.page_count}쪽)")
            return doc.render(page_no - 1, dpi)
    if page_no != 1:
        raise KeyError(f"{path} 는 이미지 한 장입니다 ({page_no}쪽 없음)")
    return imread_gray(path)


def resolve_source(source_path: str | None, source_rel: str | None, archive_root: str | Path | None) -> Path | None:
    """문서의 원본 파일 중 이 컴퓨터에서 닿는 경로. 절대경로가 없으면 archive_root + 상대경로. 둘 다 없으면 None."""
    if source_path:
        p = Path(source_path)
        if p.exists():
            return p
    if source_rel and archive_root is not None:
        q = Path(archive_root) / source_rel
        if q.exists():
            return q
    return None
