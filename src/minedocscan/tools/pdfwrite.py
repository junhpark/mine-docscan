"""합성 PDF 를 직접 쓴다 (tasks/0009 4.3 가) — PDF 라이브러리 없이.

쪽마다 그림 하나 (8비트 회색조): 복합기 스캔처럼 JPEG 바이트를 그대로(`DCTDecode`), 무손실이 필요하면 화소를 zlib 으로(`FlateDecode`).
쪽의 크기 = 그림의 화소 × 72 / dpi (pt) — 읽는 쪽(imaging/io.page_px — ceil(pt × dpi / 72 − 0.001))이 같은 화소 수를 다시 낸다.
`/ID`·만든 시각을 넣지 않는다 — 같은 입력이면 바이트까지 같다 (같은 seed 의 합성 묶음은 같은 문서 ID — pypdfium2 의 save 는 매번 다른
/ID 를 쓴다). 쪽을 떼어 붙이는 것(시험의 작은 묶음)도 다시 그리지 않고 스트림을 그대로 옮긴다 (`read_pages` — 이 모듈이 쓴 PDF 만 읽는다).
합성·시험 전용이다. 현장의 PDF 를 쓰지 않는다.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

JPEG_QUALITY = 85                   # 복합기 스캔처럼 (지금까지의 합성 묶음과 같은 품질)
HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


@dataclass(frozen=True)
class PdfPage:
    """쪽 하나: 크기(pt)와 그림 (인코딩된 스트림 그대로 — None 이면 빈 쪽)."""
    width_pt: float
    height_pt: float
    image: bytes | None = None
    filter: str = "DCTDecode"       # DCTDecode | FlateDecode
    px: tuple[int, int] = (0, 0)     # 그림의 (너비, 높이) 화소


def page_of(img: np.ndarray, dpi: int = 200, lossless: bool = False) -> PdfPage:
    """회색조 그림 한 장 → 쪽. lossless=False 면 JPEG(품질 85), True 면 화소 그대로 zlib (돌린 쪽이 바로 선 쪽을 정확히 돌린 것이어야
    할 때 — synth --intake)."""
    if img.ndim != 2 or img.dtype != np.uint8:
        raise ValueError("8비트 회색조 그림만 씁니다")
    h, w = img.shape
    if lossless:
        data, flt = zlib.compress(np.ascontiguousarray(img).tobytes(), 1), "FlateDecode"   # 1: 9 보다 2.7배 빠르고 2 % 크다
    else:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise ValueError("페이지를 인코딩할 수 없습니다")
        data, flt = buf.tobytes(), "DCTDecode"
    return PdfPage(w * 72 / dpi, h * 72 / dpi, data, flt, (w, h))


def blank_page(width_pt: float, height_pt: float) -> PdfPage:
    """그림 없는 흰 쪽 (시험 — 묶음 사이에 끼운 흰 종이)."""
    return PdfPage(width_pt, height_pt)


def _num(x: float) -> bytes:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return (s or "0").encode()


def write_pdf(path: str | Path, pages: list[PdfPage]) -> Path:
    """쪽들을 PDF 하나로. 같은 쪽이면 같은 바이트."""
    if not pages:
        raise ValueError("쪽이 없습니다")
    objs: list[bytes] = []                       # 객체 번호 = 자리 + 1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"")                                     # 1: Catalog (아래에서 채운다)
    add(b"")                                     # 2: Pages
    kids = []
    for p in pages:
        w, h = _num(p.width_pt), _num(p.height_pt)
        box = b"[0 0 " + w + b" " + h + b"]"
        if p.image is None:
            kids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox " + box + b" /Resources << >> >>"))
            continue
        pw, ph = p.px
        img = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8 "
                  b"/Filter /%s /Length %d >>\nstream\n" % (pw, ph, p.filter.encode(), len(p.image)) + p.image + b"\nendstream")
        content = b"q " + w + b" 0 0 " + h + b" 0 0 cm /Im0 Do Q"
        cont = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox " + box + b" /Resources << /XObject << /Im0 %d 0 R >> >> "
                        b"/Contents %d 0 R >>" % (img, cont)))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(HEADER)
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def write_images(path: str | Path, images: list[np.ndarray], dpi: int = 200, lossless: bool = False) -> Path:
    return write_pdf(path, [page_of(img, dpi, lossless) for img in images])


_OBJ = re.compile(rb"(\d+) 0 obj\n")


def read_pages(path: str | Path) -> list[PdfPage]:
    """이 모듈이 쓴 PDF 의 쪽들 (스트림을 디코딩하지 않고 그대로). 다른 PDF 면 ValueError."""
    data = Path(path).read_bytes()
    if not data.startswith(HEADER):
        raise ValueError("pdfwrite 가 쓴 PDF 가 아닙니다")
    objs: dict[int, tuple[bytes, bytes | None]] = {}
    pos = len(HEADER)
    while True:
        m = _OBJ.match(data, pos)
        if not m:
            break
        start = m.end()
        n = int(m.group(1))
        eol = data.index(b"\n", start)                   # 사전은 늘 한 줄 (write_pdf)
        head = data[start:eol]
        if data.startswith(b"\nstream\n", eol):
            s0 = eol + len(b"\nstream\n")
            stream = data[s0:s0 + int(re.search(rb"/Length (\d+) >>$", head).group(1))]
            end = s0 + len(stream) + len(b"\nendstream")
        else:
            stream, end = None, eol
        if not data.startswith(b"\nendobj\n", end):
            raise ValueError("pdfwrite 가 쓴 PDF 가 아닙니다")
        objs[n] = (head, stream)
        pos = end + len(b"\nendobj\n")
    kids = [int(k) for k in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", objs[2][0]).group(1))]
    pages = []
    for k in kids:
        head = objs[k][0]
        w, h = (float(v) for v in re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", head).groups())
        im = re.search(rb"/Im0 (\d+) 0 R", head)
        if im is None:
            pages.append(PdfPage(w, h))
            continue
        ihead, stream = objs[int(im.group(1))]
        pw, ph = (int(v) for v in re.search(rb"/Width (\d+) /Height (\d+)", ihead).groups())
        flt = re.search(rb"/Filter /(\w+)", ihead).group(1).decode()
        pages.append(PdfPage(w, h, stream, flt, (pw, ph)))
    return pages


def copy_pages(parts: list[tuple[str | Path, list[int]]], out: str | Path) -> Path:
    """[(원본, [쪽 번호 — 1부터])] 의 쪽들을 차례로 새 PDF 하나로 — 다시 그리지 않고 옮긴다 (같은 입력이면 바이트까지 같다)."""
    pages = []
    for src, nums in parts:
        got = read_pages(src)
        pages += [got[n - 1] for n in nums]
    return write_pdf(out, pages)
