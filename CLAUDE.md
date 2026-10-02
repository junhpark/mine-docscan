# mine-docscan — 작업 안내 (Claude Code 용)

광산 현장의 수기 문서를 스캔하면 바로 데이터베이스에 들어가게 하는 파이프라인이다.
패키지·CLI 이름은 `minedocscan`. 이 파일은 저장소에서 작업할 때 먼저 읽는 요약이고,
자세한 내용은 `docs/` 에 있다.

- `docs/ARCHITECTURE.md` — 구조, 데이터 모델, 확장 지점
- `docs/SITE_PACK.md` — 양식 정의(템플릿) 형식, 새 양식 추가 절차
- `docs/DATA.md` — 데이터가 어디에 있고 무엇을 커밋하면 안 되는지
- `docs/ROADMAP.md` — 구현된 것 / 남은 것 / 결정이 필요한 것
- `docs/PRIOR_WORK.md` — 선행 연구에서 이어받은 요구사항과 바꾼 것
- `docs/decisions/` — 왜 이렇게 했는지 (ADR)
- `docs/tasks/` — 작업 지시서. 맡은 작업의 범위·설계·수용 기준은 여기서 읽는다

## 범위

이 저장소는 **1단계 소프트웨어**다: 스캐너 → 인식 → 데이터베이스. 독립 소프트웨어로 등록할 예정이므로
혼자서 설치·실행·시험이 되어야 한다. 2단계(통합 DB, 입력 체계, 시각화)는 이 저장소의 업무 테이블
(`insp_*`, `prod_*`, `xcheck_*`, `eq_*`)을 읽어 가는 별도 작업이다. 2단계 기능을 여기에 넣지 않는다.

물질수지 관점: 현장 문서의 값은 서로 맞지 않는다. 1단계는 그 차이를 **고치지 않고 계산해서 보여 준다**
(`xcheck_*`). 조정(reconciliation)은 2단계의 일이다.

## 자주 쓰는 명령

```bash
pip install -e ".[dev]"

pytest                      # 합성 양식으로 전체 시험 (약 1분, 실데이터 불필요)
ruff check .

minedocscan synth out/demo                  # 개인정보 없는 합성 사이트 팩 + 스캔 + 정답
minedocscan run   --site out/demo/site --archive-root out/demo/scans --work-root out/demo/work
minedocscan report --work-root out/demo/work
minedocscan eval  --answers out/demo/answers.json --work-root out/demo/work

minedocscan review serve --site out/demo/site --work-root out/demo/work --queue haul-numbers --reviewer jp
                                            # 127.0.0.1:8765 — 셀을 보고 값을 입력. 기록은 <site>/reviews/reviews.jsonl
minedocscan review stats  --site … --work-root …          # 얼마나 했는지
minedocscan review export-answers answers.json --site … --work-root …
minedocscan eval --answers answers.json --target raw --only-listed --work-root …   # 기계 값을 검수값과 비교

# 실데이터 (저장소 밖 — docs/DATA.md)
export MINEDOCSCAN_SITE=…/site-packs/<현장>  MINEDOCSCAN_ARCHIVE_ROOT=…/mine-docscan  MINEDOCSCAN_WORK_ROOT=…/work
minedocscan info
minedocscan regress         # 사이트 팩의 기준 수치와 비교 (pytest -m realdata 도 같은 검사)
```

## 여섯 가지 원칙

1. **양식은 데이터다.** 양식 하나 = 사이트 팩의 `template.yaml` + 기준 이미지. 새 양식 때문에 코드를 고치지 않는다.
   코드를 고치는 것은 새 *종류*의 업무 기록(핸들러)이 필요할 때뿐이다.
2. **인쇄된 값은 읽지 않는다.** 장비명·광종·편처럼 양식에 인쇄된 값은 템플릿에서 확정한다. OCR 대상은 손으로 쓴 것뿐이다.
3. **모든 값은 출처를 가진다.** `doc_field` 에 페이지·좌표·원문·최종값·신뢰도·후보를 남긴다. 업무 테이블의 값은
   `source_field_id` 로 그 자리까지 거슬러 올라갈 수 있어야 한다.
4. **교정은 선택이지 생성이 아니다.** 교정기는 후보 중에서 고른다. 등록번호·차량번호 같은 고유값은 교정하지 않고 마스터와 맞춘다.
5. **사람은 예외만 본다.** 확신이 없는 값은 `review_status = pending` 으로 보내고 나머지는 자동 적재한다. 검수 결과는 학습 데이터가 된다.
6. **인식기는 갈아 끼우는 부품이다.** 파이프라인은 `Recognizer` 인터페이스만 안다. 백엔드는 같은 평가셋의 수치로 비교해 바꾼다.

## 패키지 지도 (`src/minedocscan/`)

| 위치 | 하는 일 |
|---|---|
| `config.py` | 설정 (환경변수 > TOML > 기본값). 경로를 코드에 적지 않는다 |
| `imaging/io.py` | 이미지·PDF 읽기/쓰기. **한글 경로 때문에 `cv2.imread/imwrite` 를 직접 쓰지 않는다** |
| `imaging/grid.py` | 표 괘선 검출 |
| `imaging/align.py` | ORB + RANSAC 으로 기준 이미지에 정합, 괘선 재검출 오차로 품질 판정 |
| `imaging/cells.py` | 셀 크롭과 잉크 비율 |
| `imaging/marks.py` | ✓ 판정 (나란한 두 칸 중 어디에 표시했나) |
| `imaging/blobs.py` | 괘선 제거 + RLSA 로 글씨 덩어리를 셀에 배정, 여러 칸에 걸친 메모 구분 |
| `forms/template.py` | 템플릿 로더·검증 |
| `forms/sitepack.py` | 사이트 팩 (템플릿·현장 옵션·페이지 라벨) |
| `forms/classify.py` | 페이지가 어느 양식인지 |
| `recognize/` | 인식 백엔드 인터페이스와 등록소 (`null`, `oracle`) |
| `correct/` | 교정 백엔드 인터페이스와 등록소 (`none`) |
| `handlers/` | 양식의 의미: 셀 → `doc_field` → 업무 테이블 (`generic`, `inspection`, `haul`) |
| `validate/crosscheck.py` | 양식 간 교차검증, 그날의 실제 배차 관측 (날짜 지정 재계산 가능) |
| `review/` | 검수: `store.py`(추가 전용 `reviews.jsonl` ↔ `doc_review`, `save()`), `queue.py`(대기열 3종), `crops.py`, `server.py` + `static/index.html`(표준 라이브러리, 127.0.0.1) |
| `store/` | `schema.sql`, `upsert()` |
| `pipeline/runner.py` | 단계 순서와 상태 기록만 안다 |
| `evaluate/` | CER·필드 정확도·자동 적재율, 실데이터 회귀 |
| `report.py` | DB 현황 요약 (회귀 테스트가 비교하는 수치) |
| `tools/synth.py` | 합성 양식·스캔·정답 생성기 |
| `tools/mktemplate.py` | 새 양식의 템플릿 뼈대 |
| `cli.py` | `minedocscan` 명령 |

## 작업 규칙

**변경은 수치로 판단한다.** 인식·정합·판정 규칙을 고쳤으면
1. `pytest` — 합성 데이터에서 정답과 정확히 같아야 한다.
2. 실데이터가 있는 환경이면 `minedocscan regress` — 기준과 달라진 수치를 확인하고, 좋아진 것이면
   `--update` 로 기준을 갱신하고 무엇이 왜 바뀌었는지 커밋 메시지에 적는다. 수치가 달라졌는데 설명이 없으면 안 된다.
3. 인식 백엔드를 붙이기 전에 `oracle` 백엔드로 CER 0 을 확인한다. 0 이 아니면 인식기가 아니라 파이프라인의 버그다.

**개인정보를 커밋하지 않는다.** 현장 문서에는 작업자 이름, 서명, 차량번호가 있다.
- 스캔 원본, 기준 이미지, 템플릿 YAML(머리글에 이름·차량번호가 들어 있다), 페이지 라벨, 정답 CSV·엑셀, 검수 기록(`reviews.jsonl`)은 전부 저장소 밖(사이트 팩·아카이브)에 둔다.
- 검수 화면의 갈무리(실제 값이 보인다)를 문서·PR·이슈에 붙이지 않는다. 서버 로그에 입력값을 찍지 않는다.
- 테스트에 필요한 이미지는 `tools/synth.py` 로 만든다. 실제 문서를 `tests/fixtures/` 에 넣지 않는다.
- 문서·코드·커밋 메시지·이슈에 실제 이름이나 차량번호를 예시로 쓰지 않는다. 합성 데이터의 값(`T01`, `V-101`, `ALPHA`)을 쓴다.
- API 키·비밀값은 환경변수로만 받는다. `.env`, `minedocscan.toml` 은 커밋되지 않는다.

**코드 규약**
- Python 3.11+, `ruff` (줄 길이 110). 주석·독스트링·문서는 한국어, 식별자는 영어.
- 좌표는 전부 템플릿 좌표계(기준 이미지 픽셀, 200 dpi). 페이지 좌표를 따로 들고 다니지 않는다.
- DB 쓰기는 `store.db.upsert()` 만 쓴다. 같은 문서를 다시 돌려도 행이 늘지 않아야 한다(멱등).
- 스키마는 SQLite 와 PostgreSQL 에서 같이 도는 문법만 쓴다. 날짜는 ISO 문자열, 불리언은 0/1.
  컬럼이 바뀌면 `store/db.py` 의 `SCHEMA_VERSION` 을 올린다 (마이그레이션 없음, 예전 DB 는 `--fresh`).
- 기계 값(`value_raw`, `has_value_raw`, `trips_raw`, `confidence`, `backend`)은 검수가 건드리지 않는다. 업무 테이블은 검수를 적용한 최종 필드 행에서 만든다 (ADR 0008).
- 검수 기록의 원본은 사이트 팩의 `reviews/reviews.jsonl` 이다. 지우거나 덮어쓰지 않는다. 테스트에서는 `reviews` 경로를 `tmp_path` 로 돌린다.
- 판정 규칙의 숫자(임계값)는 근거를 주석으로 남긴다. 실데이터에서 어떤 경우 때문에 그 값이 되었는지.
- 현장에 관한 것(장비 구분, 파일명 규칙, 제외할 광종)을 코드에 적지 않는다. 사이트 팩의 `site.toml` 로 보낸다.

## 확장하는 법 (요약)

- **새 양식** (같은 종류의 기록): `minedocscan template init <이미지> --name <이름> --roi …` → `template.yaml` 의 열·행을 채운다.
  코드 변경 없음. `docs/SITE_PACK.md`.
- **새 종류의 업무 기록**: `handlers/<이름>.py` 에 `FormHandler` 를 상속해 `load()` 를 쓰고 `handlers/__init__.py` 의 `REGISTRY` 에 등록,
  `store/schema.sql` 에 테이블과 `store/db.py` 의 `PRIMARY_KEYS` 를 추가, `tools/synth.py` 에 그 양식의 합성판과 테스트를 추가.
- **새 인식 백엔드**: `recognize/<이름>.py` 에 `recognize(crops, contexts) -> list[Recognition]` 을 구현하고 `register()`.
  무거운 의존성(torch 등)은 그 모듈 안에서만 import 하고 `pyproject.toml` 의 선택 의존성으로 넣는다.
- **새 교정 백엔드**: `correct/` 에 같은 방식으로. 후보를 내고 고르는 구조를 지킨다.

## 실데이터에서 배운 것 (다시 틀리지 않기 위해)

- ✓ 는 왼쪽 칸에서 시작해 경계선을 넘어 오른쪽 칸까지 그려진다. 잉크량 비교나 꼭짓점 위치로는 틀린다 → `imaging/marks.py` 의 규칙.
- 점검을 하지 않는 날(예: 토요일)은 체크 열 전체가 빈다. 행마다 "판정 불가"로 두지 말고 페이지 단위로 `column_unused`.
- 가로 양식은 한 페이지에 표가 여러 개라 페이지 전체 괘선 검출이 짧은 괘선을 놓친다 → 표(region)별 ROI 검출.
- 정합 품질은 표별 괘선 오차의 **중앙값**으로 본다. 스캔 가장자리에서 잘린 괘선 하나가 평균을 망친다.
- 표 위에 여러 칸에 걸쳐 쓴 메모는 셀 값이 아니다 → `imaging/blobs.py` 가 덩어리 폭으로 구분한다.
- 행렬 양식에 인쇄된 운전자·차량번호는 양식을 만든 당시 상태로 굳어 있다. 사람도 차도 바뀐다.
  두 양식을 잇는 키는 행렬의 **열 자리(slot)** 이고, 그날의 실제 배차는 `eq_assignment_obs` 에 관측값으로 남긴다.
- 하루에 행렬 양식이 여러 장일 수 있다(상차 장비마다 한 장). 그날의 모든 장을 합쳐서 비교한다.
- 양식의 여백 행에 손으로 장비를 추가해 적는 날이 있다. `doc_field` 에는 남지만 업무 테이블로 가는 규칙은 아직 없다.
- PDF 는 200 dpi 회색조로 직접 렌더링한다. 렌더링 경로를 바꾸면 체크 판정 몇 개가 뒤집힌다 — 회귀 기준을 다시 잡아야 한다.
- 스캔 원본은 300 dpi 다. 정합과 판정은 200 dpi 로 충분하지만, 인식기에 넘기는 크롭은 원본 해상도가 나을 수 있다 (아직 비교하지 않았다).
- 언어모델에 문장을 다시 쓰게 하면 긴 셀에서 항목 순서가 바뀌고 수량이 달라진다 (선행 연구의 비교표). 교정은 후보 선택 + 숫자 불변 검사.

## 하지 말 것

- 범용 표 인식 모델로 셀을 찾으려 하지 않는다. 양식은 고정이고 정합이 더 정확하다 (ADR 0001).
- 인쇄된 머리글 값을 사실로 믿는 조인을 만들지 않는다 (ADR 0004).
- 교차검증 불일치를 자동으로 "맞춰" 넣지 않는다. 보여 주고 검수로 보낸다 (ADR 0006).
- 합성 데이터의 수치로 한글 손글씨 인식률을 말하지 않는다. 인식률은 실데이터 평가셋으로만 말한다.
- 외부 API 를 부르는 인식·교정 백엔드를 만들지 않는다. 현장은 외부 네트워크 없이 운용한다 (ADR 0003).
