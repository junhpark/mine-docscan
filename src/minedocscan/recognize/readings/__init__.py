"""소수·시각 모델 (계기 값, 시각, 근무 시각 범위) — tasks/0011.

  model.py    추론 (torch 없음): 글씨 띠, 형식으로 고르는 해독, 카드
  backend.py  인식 백엔드 "readings": 카드의 형식·규격·온도·자동 적재 기준으로 형식 칸을 읽는다
  data.py     학습·평가용 크롭 폴더 읽기 (형식 칸 줄만, 정답의 표기들)
  calib.py    여러 표기의 CTC 우도·온도, 임계값별 자동 적재율·오류율, 성적
  train.py    학습 (torch — 여기서만 import 한다), --cv, ONNX 내보내기, 카드
  evaluate.py 크롭 단위 평가 (recognizer eval)

설정:
  [recognize.by_format]
  reading = "readings"
  time_range = "readings"
  [recognize.readings]
  model = "readings-v1"          # <site>/models/<이름>/ 또는 폴더 경로. site.toml 의 같은 항목보다 설정 파일이 이긴다
  auto_accept_conf = 0.97        # (선택) 모델 카드의 기준 대신
"""
from __future__ import annotations


def make(settings=None, site=None):
    """등록소의 팩토리 (recognize.get_recognizer). 모델이 지정되지 않았거나 없으면 오류로 멈춘다 — null 로 물러나지 않는다."""
    from .backend import ReadingsRecognizer

    return ReadingsRecognizer.from_settings(settings, site)
