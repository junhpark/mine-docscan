"""숫자 인식기 (운반 횟수) — tasks/0003.

  model.py    추론 (torch 없음): 입력 전처리, CTC 답 읽기, ONNX 를 OpenCV 로, 모델 폴더·카드
  backend.py  인식 백엔드 "digits": 카드의 규격·온도·자동 적재 기준으로 숫자 칸을 읽는다
  data.py     학습·평가용 크롭 폴더 읽기 (test 거절, 규격 하나), 검증 날짜
  calib.py    온도, 임계값별 자동 적재율·오류율(윌슨 구간), 기준 고르기, 정확도
  train.py    학습 (torch — 여기서만 import 한다), ONNX 내보내기, 모델 카드

설정:
  [recognize.by_kind]
  handwritten_number = "digits"
  [recognize.digits]
  model = "digits-v1"            # <site>/models/<이름>/ 또는 폴더 경로. site.toml 의 같은 항목보다 설정 파일이 이긴다
  auto_accept_conf = 0.97        # (선택) 모델 카드의 기준 대신
"""
from __future__ import annotations


def make(settings=None, site=None):
    """등록소의 팩토리 (recognize.get_recognizer). 모델이 지정되지 않았거나 없으면 오류로 멈춘다 — null 로 물러나지 않는다."""
    from .backend import DigitsRecognizer

    return DigitsRecognizer.from_settings(settings, site)
