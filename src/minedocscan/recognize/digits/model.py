"""숫자 인식기의 추론 쪽 — torch 없이 돈다 (tasks/0003 4.2, 4.3).

모델: 합성곱·배치 정규화·ReLU·풀링만 쓴 작은 망 → 세로를 최대 풀링으로 접어 가로 T 칸의 문자 점수 → CTC.
학습(train.py, torch)이 ONNX 로 내보내고, 여기서는 그 파일을 OpenCV(cv2.dnn)로 읽는다. 현장 PC 에 새 의존성이 없다.

입력: 크롭(회색조) → 고정 크기(INPUT_W × INPUT_H)로 줄이고 → 종이·잉크로 밝기를 맞춘 잉크 채널 + 가로·세로 위치 채널 2개.
위치 채널은 "가운데 칸의 글씨"와 "괘선을 넘어 들어온 이웃 칸의 글씨"를 가르는 데 쓰인다 (세로를 최대 풀링으로 접기
전에 위치를 알아야 한다). 학습과 추론이 이 파일의 같은 함수(resize_input, normalize)를 쓴다.

출력 문자: 빈칸(blank, CTC) + 0–9 + "?"(거절). 답은 세 가지다 (4.3):
  숫자열 — 앞의 0 을 뗀 십진수 ("07" → "7")
  빈 칸  — "" (잉크는 있지만 이 칸의 값이 아니다: X 표, 지운 것, 이웃 칸 글씨, 메모)
  거절   — "?" (숫자 같지만 읽을 수 없다. 검수의 illegible 로 학습한다)
신뢰도 = 그 답으로 접히는 모든 경로의 확률 합 (온도 하나로 보정, 카드의 temperature).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path

import cv2
import numpy as np

CHARS = "0123456789?"
REJECT = "?"
BLANK = 0
N_CLASSES = 1 + len(CHARS)
INPUT_W, INPUT_H = 96, 32
N_INPUT_CHANNELS = 3
KINDS = ("handwritten_number",)            # 이 모델이 읽는 칸 종류
TOP_K = 5                                  # 후보로 남기는 답의 수 (9절 기본값)
CARD_KEYS = ("name", "created_at", "code_version", "spec", "input", "chars", "kinds", "architecture", "train_args",
             "seed", "data", "validation", "temperature", "auto_accept", "libraries", "model_sha256")


# ── 입력 ───────────────────────────────────────────────────────────────────
def resize_input(gray: np.ndarray) -> np.ndarray:
    """크롭 → 모델 입력 크기의 회색조 uint8. 칸마다 크롭 크기가 달라도 칸의 상대 위치는 같다
    (여유 = 행 높이의 절반이라 칸은 늘 크롭 세로의 가운데 절반)."""
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    h, w = gray.shape
    if (w, h) == (INPUT_W, INPUT_H):
        return gray.copy()
    shrink = w > INPUT_W or h > INPUT_H
    return cv2.resize(gray, (INPUT_W, INPUT_H), interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)


_YS = np.repeat(np.linspace(-1, 1, INPUT_H, dtype=np.float32)[:, None], INPUT_W, axis=1)
_XS = np.repeat(np.linspace(-1, 1, INPUT_W, dtype=np.float32)[None, :], INPUT_H, axis=0)


def normalize(small: np.ndarray, position: bool = True) -> np.ndarray:
    """모델 입력 크기의 회색조 → (3, H, W) float32: 잉크 채널, 세로 위치, 가로 위치.
    잉크 = (종이 - 화소) / 대비. 종이는 중앙값, 대비는 종이와 가장 진한 1 % 의 차 — 단 48 미만으로는 두지 않는다
    (거의 빈 칸에서 종이의 잡음을 잉크로 키우지 않게. 연한 잉크는 최대 약 5배까지 키운다)."""
    g = small.astype(np.float32)
    paper = float(np.median(g))
    dark = float(np.percentile(g, 1))
    ink = np.clip((paper - g) / max(paper - dark, 48.0), -0.3, 1.2)
    if not position:                         # 메타 필드 모델: 위치 채널을 0 으로 (meta/train.py — 넓은 필드에서 지름길이 되었다)
        z = np.zeros_like(_YS)
        return np.stack([ink, z, z]).astype(np.float32)
    return np.stack([ink, _YS, _XS]).astype(np.float32)


def preprocess(crop: np.ndarray, position: bool = True) -> np.ndarray:
    """크롭 하나 → 모델 입력 (1, 3, H, W). position=False 면 위치 채널이 0 (카드의 input.position)."""
    return normalize(resize_input(crop), position)[None]


# ── 답 ────────────────────────────────────────────────────────────────────
def encode(text: str) -> list[int]:
    """정답 문자열 → CTC 목표 (blank 다음부터 CHARS 순서)."""
    return [CHARS.index(c) + 1 for c in text]


def normalize_answer(s: str) -> str:
    """경로가 접힌 문자열 → 답. "?" 가 섞이면 거절, 숫자는 앞의 0 을 뗀다 ("07" → "7", "0" 은 "0")."""
    if REJECT in s:
        return REJECT
    if s == "":
        return ""
    return str(int(s))


def answer_kind(text: str) -> str:
    """value | empty | reject"""
    return "empty" if text == "" else "reject" if text == REJECT else "value"


def log_softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    """(C, T) 점수 → (T, C) 로그 확률."""
    z = logits.T.astype(np.float64) / float(temperature)
    z = z - z.max(axis=1, keepdims=True)
    return z - np.log(np.exp(z).sum(axis=1, keepdims=True))


def _lse(a: float, b: float) -> float:
    if a == -math.inf:
        return b
    if b == -math.inf:
        return a
    m = max(a, b)
    return m + math.log(math.exp(a - m) + math.exp(b - m))


def beam_search(logp: np.ndarray, beam: int = 12, prune: float = -10.0) -> list[tuple[str, float]]:
    """CTC 접두사 빔 탐색. (T, C) 로그 확률 → [(문자열, 로그 확률)] 확률순. 확률은 그 문자열로 접히는 경로의 합이다
    (빔에서 떨어진 경로만큼 조금 모자랄 수 있다). prune: 한 칸에서 이보다 낮은 로그 확률의 문자는 보지 않는다."""
    beams: dict[tuple[int, ...], tuple[float, float]] = {(): (0.0, -math.inf)}     # 접두사 → (blank 로 끝남, 문자로 끝남)
    for t in range(logp.shape[0]):
        lp = logp[t]
        cand = [c for c in range(1, lp.shape[0]) if lp[c] > prune]
        nxt: dict[tuple[int, ...], list[float]] = {}
        for prefix, (pb, pnb) in beams.items():
            tot = _lse(pb, pnb)
            e = nxt.setdefault(prefix, [-math.inf, -math.inf])
            e[0] = _lse(e[0], tot + lp[BLANK])
            last = prefix[-1] if prefix else None
            for c in cand:
                ext = nxt.setdefault(prefix + (c,), [-math.inf, -math.inf])
                if c == last:
                    e[1] = _lse(e[1], pnb + lp[c])                     # 같은 문자가 이어짐 → 접두사 그대로
                    ext[1] = _lse(ext[1], pb + lp[c])                  # blank 를 사이에 둔 반복 → 한 글자 더
                else:
                    ext[1] = _lse(ext[1], tot + lp[c])
        ranked = sorted(nxt.items(), key=lambda kv: -_lse(*kv[1]))[:beam]
        beams = {k: (v[0], v[1]) for k, v in ranked}
    out = [("".join(CHARS[c - 1] for c in p), _lse(pb, pnb)) for p, (pb, pnb) in beams.items()]
    return sorted(out, key=lambda x: -x[1])


def read_answers(logits: np.ndarray, temperature: float = 1.0, top: int = TOP_K) -> list[tuple[str, float]]:
    """(C, T) 점수 → [(답, 확률)] 확률순, 최대 top 개. 같은 답으로 정규화되는 문자열("07"·"7")의 확률은 더한다."""
    merged: dict[str, float] = {}
    for s, lp in beam_search(log_softmax(logits, temperature)):
        a = normalize_answer(s)
        merged[a] = merged.get(a, 0.0) + math.exp(lp)
    return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


# ── ONNX 파일 (OpenCV) ─────────────────────────────────────────────────────
class OnnxNet:
    """model.onnx 를 cv2.dnn 으로 읽는다. 배치는 1 로 고정해 내보냈다 (동적 배치는 cv2.dnn 에서 출력 모양이 달라졌다 — 4.2)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.net = cv2.dnn.readNetFromONNX(str(self.path))

    def logits(self, x: np.ndarray) -> np.ndarray:
        """(1, 3, H, W) → (C, T)."""
        self.net.setInput(np.ascontiguousarray(x, dtype=np.float32))
        out = np.asarray(self.net.forward())
        return out.reshape(N_CLASSES, -1)

    def logits_many(self, xs: np.ndarray) -> np.ndarray:
        """(N, 3, H, W) → (N, C, T), 한 장씩."""
        return np.stack([self.logits(x[None]) for x in xs]) if len(xs) else np.zeros((0, N_CLASSES, 0), np.float32)


# ── 모델 폴더와 카드 ────────────────────────────────────────────────────────
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def models_dir(site_root: str | Path) -> Path:
    return Path(site_root) / "models"


def resolve_model(ref: str, site_root: str | Path | None) -> Path:
    """모델 이름(사이트 팩의 models/<이름>) 또는 폴더 경로 → 모델 폴더. 없으면 무엇이 없는지 말한다."""
    p = Path(ref).expanduser()
    looks_like_path = any(sep in ref for sep in ("/", "\\")) or ref.startswith((".", "~")) or p.is_absolute()
    if looks_like_path:
        d = p
    elif site_root is not None:                              # 이름은 사이트 팩의 models/<이름> (현재 폴더의 같은 이름보다 먼저)
        d = models_dir(site_root) / ref
    else:
        raise FileNotFoundError(f"숫자 모델 '{ref}' 을 찾을 사이트 팩이 없습니다 (MINEDOCSCAN_SITE 또는 [paths] site). "
                                "폴더를 직접 주려면 경로로 (예: ./models/<이름>)")
    missing = [n for n in ("model.onnx", "card.json") if not (d / n).is_file()]
    if missing:
        have = ""
        if site_root is not None and models_dir(site_root).is_dir():
            names = sorted(x.name for x in models_dir(site_root).iterdir()
                           if (x / "card.json").is_file() and not x.name.startswith("."))      # 학습 중의 임시 폴더는 빼고
            have = f" — 사이트 팩에 있는 모델: {', '.join(names) if names else '없음'}"
        raise FileNotFoundError(f"모델 폴더에 {', '.join(missing)} 이(가) 없습니다: {d}{have}")
    return d


def load_card(model_dir: str | Path, check_hash: bool = True) -> dict:
    d = Path(model_dir)
    card = json.loads((d / "card.json").read_text(encoding="utf-8"))
    lacking = [k for k in CARD_KEYS if k not in card]
    if lacking:
        raise ValueError(f"모델 카드에 항목이 없습니다: {lacking} ({d / 'card.json'})")
    if card["chars"] != CHARS:
        raise ValueError(f"모델 카드의 문자({card['chars']!r})가 이 코드({CHARS!r})와 다릅니다 — 이 버전으로 다시 학습하세요")
    inp = card["input"]
    if (inp.get("width"), inp.get("height"), inp.get("channels")) != (INPUT_W, INPUT_H, N_INPUT_CHANNELS):
        raise ValueError(f"모델 카드의 입력({inp})이 이 코드의 입력({INPUT_W}×{INPUT_H}×{N_INPUT_CHANNELS})과 다릅니다")
    if check_hash and sha256_file(d / "model.onnx") != card["model_sha256"]:
        raise ValueError(f"model.onnx 가 카드에 적힌 것(SHA-256)과 다릅니다: {d}")
    return card


def card_threshold(card: dict) -> float:
    """카드의 자동 적재 기준. 검증 날짜에서 목표를 만족하는 임계값이 없었으면 inf (= 자동 적재 없음, 4.6)."""
    t = (card.get("auto_accept") or {}).get("threshold")
    return math.inf if t is None else float(t)


# 학습 중의 임시 폴더: 모델 폴더와 같은 부모에 `.<이름>.tmp-<pid>`. 다 만든 뒤 <이름> 으로 바꾼다 (반쯤 쓴 모델이 보이지 않게)
STAGING = re.compile(r"^\.(?P<name>.+)\.tmp-(?P<pid>\d+)$")


def staging_dir(out_dir: str | Path) -> Path:
    """학습 결과를 쓸 임시 폴더를 새로 만든다. 그 전에 같은 부모의 주인 없는 임시 폴더를 치운다 — 학습을 중간에 끊으면
    (창을 닫거나 프로세스를 죽이면 정리 코드가 돌지 않는다) 남는다 (tasks/0005 단계 1)."""
    out_dir = Path(out_dir)
    clean_orphan_staging(out_dir.parent)
    tmp = out_dir.parent / f".{out_dir.name}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    return tmp


def clean_orphan_staging(parent: str | Path) -> list[str]:
    """parent 의 `.<이름>.tmp-<pid>` 중 그 pid 의 프로세스가 없는 것을 지운다. 지운 폴더 이름의 목록.
    살아 있는 프로세스의 것(다른 창에서 학습 중)은 두고, 알 수 없으면 둔다 (지우는 쪽으로 틀리지 않게)."""
    parent = Path(parent)
    removed = []
    if not parent.is_dir():
        return removed
    for p in sorted(parent.iterdir()):
        m = STAGING.match(p.name)
        if m and p.is_dir() and int(m["pid"]) != os.getpid() and not _pid_alive(int(m["pid"])):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p.name)
    return removed


def _pid_alive(pid: int) -> bool:
    """그 pid 의 프로세스가 있는가. Windows 에서 os.kill(pid, 0) 은 프로세스를 끝내 버리므로 쓰지 않는다 (OpenProcess 로 본다)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return kernel32.GetLastError() == 5                    # ERROR_ACCESS_DENIED: 있지만 볼 권한이 없다
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                                            # 권한 없음 등: 있다고 본다
        return True
    return True


def list_models(site_root: str | Path) -> list[dict]:
    """사이트 팩의 모델과 카드 요약 (recognizer list). 카드를 읽을 수 없는 폴더는 error 를 단다.
    점으로 시작하는 폴더(학습 중이거나 끊긴 학습의 임시 폴더 `.<이름>.tmp-<pid>`)는 모델이 아니다 — 내지 않는다."""
    from ...imaging.cropspec import CropSpec

    root = models_dir(site_root)
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []:
        row = {"name": d.name, "path": str(d), "created_at": None, "spec": "-", "threshold": "-", "train_cells": 0,
               "train_dates": 0, "synthetic_cells": 0, "val_source": "-", "val_cells": 0, "val_value_acc": None,
               "val_empty_acc": None}
        try:
            raw = json.loads((d / "card.json").read_text(encoding="utf-8")) if (d / "card.json").is_file() else {}
            if raw.get("meta"):                                # 메타 필드 모델 (tasks/0004): 읽는 법·키, 읽기 수치
                from ..meta.model import MetaModel

                card = MetaModel(d).card
                aa, v, m = card["auto_accept"], card["validation"], card["meta"]
                row.update(created_at=card["created_at"], spec=CropSpec.from_dict(card["spec"]).describe(),
                           threshold=(f"{aa['threshold']} (상한 {aa['upper95']:.1%})" if aa.get("met") else "없음"),
                           train_cells=card["data"]["train"]["cells"], train_dates=card["data"]["train"]["dates"],
                           synthetic_cells=card["data"]["synthetic"]["cells"], val_source=v["source"], val_cells=v["reads"],
                           val_value_acc=v["score"]["accuracy"], val_empty_acc=None, reader=m["reader"], keys=m["keys"],
                           classes={k: c["n"] for k, c in m["classes"].items()} if m["reader"] == "digits" else m["classes"])
                out.append(row)
                continue
            card = load_card(resolve_model(str(d), site_root))
        except (OSError, ValueError, KeyError) as e:
            row["error"] = str(e)
            out.append(row)
            continue
        aa, v, dt = card["auto_accept"], card["validation"], card["data"]
        row.update(created_at=card["created_at"], spec=CropSpec.from_dict(card["spec"]).describe(),
                   threshold=(f"{aa['threshold']} (상한 {aa['upper95']:.1%})" if aa.get("upper95") is not None
                              else aa["threshold"]) if aa.get("met") else "없음", train_cells=dt["train"]["cells"],
                   train_dates=dt["train"]["dates"], synthetic_cells=dt["synthetic"]["cells"], val_source=v["source"],
                   val_cells=v["cells"], val_value_acc=v["score"]["value"]["accuracy"],
                   val_empty_acc=v["score"]["empty"]["accuracy"])
        out.append(row)
    return out
