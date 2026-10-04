"""장비 마스터: 점검표 템플릿의 장비 행이 마스터다. 핸들러(점검표·가동 일보)와 사이트 팩이 같이 쓴다.

  · 장비 키 = 점검표 템플릿의 행 키(key). 같은 키는 언제 어디서 돌려도 같은 UUID 가 된다 (equipment_id).
  · 가동 일보에 손으로 적는 장비명은 쪽 메타 `equipment`(META_KEY) 다. 이름 → 장비 키는 사이트 팩의 대응표
    `site.toml [equipment.aliases]` 로만 정한다. 표에 없는 이름은 ID 가 없다 — 비슷한 이름으로 맞추지 않는다 (tasks/0005 4.5).

이 모듈은 handlers/ 를 가져오지 않는다 (forms/sitepack.py → handlers → forms/sitepack.py 의 순환을 피하려고).
"""
from __future__ import annotations

import uuid

EQ_NAMESPACE = uuid.UUID("5f1c2a2e-7d0b-4a7f-9a3e-2b1e2c3d4e5f")
META_KEY = "equipment"                  # 가동 일보에 적힌 장비명의 쪽 메타 키


def equipment_id(equipment_key: str) -> str:
    """같은 장비 키는 언제 어디서 돌려도 같은 UUID 가 된다."""
    return str(uuid.uuid5(EQ_NAMESPACE, equipment_key))


def layout(tpl) -> tuple[str, str, str, str]:
    """점검표 템플릿의 (표, 유 칸, 무 칸, 점검내역 칸) 이름 — handler_options 와 기본값."""
    opt = tpl.handler_options
    return (opt.get("region") or tpl.regions[0]["name"], opt.get("yes_column", "abnormal_yes"),
            opt.get("no_column", "abnormal_no"), opt.get("text_column", "remark"))


def is_equipment_row(r: dict) -> bool:
    """장비 행인가: 행 키가 있고 모델이나 등록번호가 있다. 아니면 양식의 여백 행 (insp_daily 로 가지 않는다)."""
    model, reg = str(r.get("model", "") or ""), str(r.get("registration", "") or "")
    return bool(str(r.get("key", ""))) and bool(model or reg.strip("-"))


def master_keys(templates) -> set[str]:
    """마스터의 장비 키: 점검표(handler inspection) 템플릿의 장비 행 키 전부."""
    keys: set[str] = set()
    for t in templates:
        if t.handler != "inspection" or not t.regions:
            continue
        region = layout(t)[0]
        try:
            rows = t.region(region)["rows"]
        except KeyError:
            continue
        keys |= {str(r.get("key", "")) for r in rows if is_equipment_row(r)}
    return keys
