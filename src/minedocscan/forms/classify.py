"""양식 분류: 페이지가 어느 템플릿인지 정한다.

각 템플릿의 기준 이미지와 ORB 정합을 시도해 RANSAC 인라이어가 가장 많은 템플릿을 고른다.
인쇄된 양식(제목·괘선·고정 문구)이 특징점의 대부분이라 수기 내용과 무관하게 동작한다.
서식이 같은 양식(제목만 다른 경우)은 한 템플릿으로 두고 제목 필드로 구분한다.
같은 양식의 개정판은 모양으로 가릴 수 없다(여유가 1 에 가깝다). 쪽의 날짜를 알면 그날 유효한 템플릿만 후보로 준다
(candidates) — forms/sitepack.templates_for().
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .template import Template


@dataclass
class ClassResult:
    template: str | None
    scores: dict[str, int]
    margin: float          # 1위 / 2위 인라이어 비율. 낮으면 검수 권장


class FormClassifier:
    def __init__(self, templates: list[Template], scale: float = 0.5, nfeatures: int = 3000):
        self.scale = scale
        self.orb = cv2.ORB_create(nfeatures=nfeatures)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.refs = {t.name: self._feats(t.reference) for t in templates}

    def _feats(self, gray: np.ndarray):
        g = cv2.resize(gray, None, fx=self.scale, fy=self.scale)
        return self.orb.detectAndCompute(g, None)

    def _inliers(self, k1, d1, k2, d2) -> int:
        if d1 is None or d2 is None:
            return 0
        knn = self.bf.knnMatch(d1, d2, k=2)
        good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < 12:
            return 0
        src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
        return int(mask.sum()) if mask is not None else 0

    def classify(self, gray: np.ndarray, min_inliers: int = 60, candidates: list[str] | None = None) -> ClassResult:
        k1, d1 = self._feats(gray)
        refs = self.refs if candidates is None else {n: self.refs[n] for n in candidates if n in self.refs}
        scores = {name: self._inliers(k1, d1, k2, d2) for name, (k2, d2) in refs.items()}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            return ClassResult(None, {}, 0.0)
        best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        return ClassResult(best[0] if best[1] >= min_inliers else None, scores, best[1] / max(second, 1))
