# 작업 지시서 0001 — 최소 검수 도구

상태: **단계 1–5 구현** (2026-10-02, 브랜치 `feat/review-tool`). 단계 6 과 7절(실데이터 확인)은 남아 있다.
범위나 순서를 바꾸려면 코드보다 이 파일을 먼저 고친다.
관련: [ROADMAP.md](../ROADMAP.md) M2, [ADR 0007](../decisions/0007-labels-by-review.md), [ADR 0006](../decisions/0006-show-discrepancies.md)

## 0. 이 문서를 쓰는 법

Claude Code 에서 이렇게 시작한다.

> `CLAUDE.md` 와 `docs/tasks/0001-review-tool.md` 를 읽고 단계 1부터 진행해. 단계가 끝날 때마다 멈추고 결과를 보고해.

- 브랜치 `feat/review-tool` 에서 작업한다. 단계마다 커밋하고, 단계 5까지 끝나면 `main` 으로 PR 을 연다.
- 단계마다 `pytest` 와 `ruff check .` 가 통과해야 다음으로 간다.
- 4절 "이미 정한 것"을 바꿔야 할 이유가 생기면 구현하지 말고 먼저 묻는다.
- 9절의 열린 질문은 해당 단계에 들어가기 전에 묻는다. 답이 없으면 적힌 기본값으로 간다.

## 1. 왜 만드는가

운반 횟수(한두 자리 숫자) 인식기를 고르려면 정답이 필요한데, 스캔과 같은 기간의 현장 입력 자료는 구할 수 없다.
그래서 정답을 직접 만든다: 셀 이미지를 보여 주고 사람이 값을 입력한다. 이 도구는 나중에 현장에서 쓰는 검수 화면의
최소 형태이기도 하다. 정답 만들기와 운영 중 검수는 같은 도구, 같은 데이터다.

지금 구조에는 사람이 입력한 값이 들어갈 자리가 없다. `doc_field.review_status` 에 `reviewed` 라는 값은 정의되어 있지만
쓰는 코드가 없고, 파이프라인을 다시 돌리면 `upsert` 가 행을 덮어쓴다. 사람이 입력한 값은 다시 만들 수 없는 유일한 데이터이므로
이것부터 고친다.

## 2. 끝났을 때 되는 일

```bash
# 묶음 PDF 를 돌려 둔 상태에서
minedocscan review serve --queue haul-numbers --n 1500 --reviewer jp
#  → 브라우저에서 http://127.0.0.1:8765 : 셀 이미지가 하나씩 뜨고, 숫자를 치고 Enter 하면 다음 셀로 간다

minedocscan review stats                       # 얼마나 했는지 (날짜·양식·판정별)
minedocscan run DB_scans --fresh               # DB 를 지우고 다시 돌려도 입력한 값이 그대로 다시 붙는다
minedocscan review export-answers answers.json # 검수값 → 정답 파일
minedocscan eval --answers answers.json --target raw   # 기계가 읽은 값을 검수값과 비교
minedocscan report                             # 교차검증 일치율 (기계 값 기준 / 최종 값 기준)
```

## 3. 범위

**이번에 한다**
- 검수 기록의 저장과 재적용 (단계 1–2)
- 대기열: 운반 숫자 표본, 교차검증 불일치, 검수 대기(`pending`) (단계 3)
- 로컬 웹 화면: 숫자·텍스트 입력 (단계 4)
- 검수값 대비 평가, 일치율 (단계 5)
- 페이지 필드(차량번호·작성자) 검수로 수동 라벨 대체 (단계 6 — 단계 5까지 PR 을 낸 뒤 이어서)

**이번에 하지 않는다**
- ✓(유/무) 쌍의 검수 화면, 두 사람 교차 입력, 정합 이미지 전체 위에 표시하는 문서 단위 화면
- 인식 백엔드. 이 작업이 끝나도 인식기는 `null` 이다 — 평가 틀만 준비한다
- 로그인·다중 사용자·원격 접속
- 판정 규칙(정합·체크·잉크·덩어리) 변경. 회귀 수치의 기존 항목은 하나도 바뀌면 안 된다

## 4. 이미 정한 것

### 4.1 검수 기록의 원본은 사이트 팩의 추가 전용 파일이다

- 위치: `<site>/reviews/reviews.jsonl` (설정 `[paths] reviews`, 환경변수 `MINEDOCSCAN_REVIEWS` 로 바꿀 수 있다).
- 한 줄 = 검수 한 건. 고치는 것도 새 줄을 추가한다. 지우거나 덮어쓰지 않는다. 한 필드의 유효한 값은 가장 최근 줄이다.
- DB 의 `doc_review` 테이블은 이 파일의 사본이다. `WORK_ROOT` 는 언제든 지울 수 있어야 하므로(DATA.md) 사람이 입력한 값을 DB 에만 두지 않는다.
- 저장 순서: 파일에 먼저 쓰고(flush) 그다음 DB. 파이프라인과 검수 서버는 시작할 때 파일을 `doc_review` 로 읽어 들인다(멱등).
- 키는 `field_id` (`<문서 해시>-p<쪽>:<표>:<열>:<행>`). 같은 파일이면 언제 돌려도 같다. 같은 종이를 다시 스캔하면 다른 문서다 — 받아들인다.

한 줄의 형식:

```json
{"review_id": "…", "field_id": "ab12…-p2:haul:trips_day:3",
 "verdict": "value", "value": "7", "reviewer": "jp", "reviewed_at": "2030-01-08T01:02:03Z", "note": "",
 "source": "scan_2030-01-07#2", "template": "synth_haul_log", "region": "haul",
 "field_name": "trips_day", "row_no": 3, "row_key": "ORE|L3", "bbox": [604, 694, 846, 766],
 "machine": {"has_value": 1, "value_raw": "", "backend": "null", "confidence": 0.0}}
```

`source` 부터 `bbox` 까지는 DB 없이도 사람이 읽고 학습 데이터를 만들 수 있게 하는 문맥이다.
`machine` 은 검수 당시 기계가 낸 값이다. 템플릿을 고쳐 `bbox` 가 달라진 검수 기록은 `review stats` 에서 건수를 따로 보여 준다(적용은 한다).

### 4.2 판정은 세 가지다

| `verdict` | 뜻 | `doc_field` |
|---|---|---|
| `value` | 종이에 이렇게 적혀 있다 | `value_final` = 입력값, `has_value` = 1, `review_status` = `reviewed` |
| `empty` | 빈 칸이다 | `value_final` = `''`, `has_value` = 0, `review_status` = `reviewed` |
| `illegible` | 읽을 수 없다 | 값은 그대로, `review_status` = `pending`. 대기열에는 다시 나오지 않고 정답에도 들어가지 않는다 |

### 4.3 기계가 낸 값과 최종 값을 따로 둔다

- `doc_field.value_raw`, `confidence`, `backend` 는 언제나 기계의 것이다. 검수해도 바뀌지 않는다.
- `doc_field.has_value_raw` (새 컬럼): 기계가 판단한 값 유무. `has_value` 는 최종.
- `prod_haul.trips_raw` (새 컬럼): 기계가 읽은 횟수. `trips` 는 최종.
- 검수된 셀도 재실행 때 인식기를 **돌린다**. 그래야 새 인식기의 `value_raw` 를 검수값과 비교할 수 있다.

### 4.4 업무 테이블은 최종 값에서 만든다

- 핸들러는 `doc_field` 행을 만든 뒤 검수를 적용하고, 그 최종 행에서 업무 테이블 행을 만든다.
- 행 단위 상태: 구성 필드 중 하나라도 `pending` 이면 `pending`, 아니고 하나라도 `reviewed` 면 `reviewed`, 아니면 `auto`.
- 화면에서 저장하면 파이프라인을 다시 돌리지 않고도 업무 테이블과 그날의 교차검증이 바로 갱신된다.
- **불변식**: 저장 직후의 DB 는, 같은 검수 파일을 가지고 처음부터 다시 돌린 DB 와 같아야 한다 (`build_report` 와 업무 테이블 내용이 같다). 테스트로 고정한다.

### 4.5 검수는 옮겨 적기다. 맞추기가 아니다

- 검수자는 **종이에 적힌 그대로** 입력한다. 두 문서의 값이 달라도 각각 적힌 대로 입력한다. 어느 쪽이 맞는지는 정하지 않는다 (ADR 0006).
- 정답을 만드는 대기열(`haul-numbers`, `mismatch`)에서는 기계가 읽은 값을 **보여 주지 않는다**. 보여 주면 그 값에 끌린다.
- 운영용 대기열(`pending`)에서는 기계 값을 입력창에 미리 채운다. 맞으면 Enter 만 치면 된다.

### 4.6 화면은 표준 라이브러리로 만든다

- `http.server` + 한 장짜리 HTML(인라인 CSS·JS). 새 실행 의존성을 추가하지 않는다. CDN·외부 글꼴·외부 스크립트를 쓰지 않는다 (현장은 외부 네트워크가 없다).
- `127.0.0.1` 에만 바인딩한다. 화면에 실제 이름과 차량번호가 보인다.
- 단일 스레드 서버로 충분하다 (SQLite 연결 하나).
- 이미지는 `WORK_ROOT/aligned/` 의 정합 이미지에서 요청이 올 때 잘라 낸다. 따로 저장하지 않는다. 읽기는 `imaging/io.py` 를 쓴다.

### 4.7 스키마가 바뀐다

- 컬럼이 늘어나므로 예전 DB 파일과 맞지 않는다. 마이그레이션은 만들지 않는다 (ADR 0005). 대신 스키마 버전을 DB 에 기록하고,
  버전이 다르면 "`--fresh` 로 다시 만드세요"라는 분명한 오류를 낸다. 검수 기록은 파일에 있으므로 잃지 않는다.

## 5. 단계

### 단계 1 — 검수 저장소

할 일
- `store/schema.sql`: `doc_review` 테이블, `doc_field.has_value_raw`, `prod_haul.trips_raw`, 스키마 버전. `store/db.py` 의 `PRIMARY_KEYS` 와 버전 검사.
- `config.py`: 검수 파일 경로 (`reviews`, 기본 `<site>/reviews/reviews.jsonl`).
- `review/store.py` (새 패키지 `src/minedocscan/review/`):
  - `Review` 데이터클래스, `append(path, review)`, `load(path)`, `import_into(con, path)` (멱등), `effective(con, page_id=None)` (필드별 최신).
  - `review_id` 는 `field_id`, `reviewed_at`, `reviewer` 에서 결정되게 만든다.
  - 깨진 줄(쓰다 끊긴 마지막 줄)은 건너뛰고 건수를 알린다. 파일 전체를 버리지 않는다.

수용 기준
- 같은 파일을 두 번 읽어 들여도 `doc_review` 행 수가 같다.
- 한 필드에 검수가 세 건이면 `effective` 는 가장 늦은 것을 준다.
- 한글 경로, 한글 값이 왕복된다 (UTF-8, `ensure_ascii=False`).
- 예전 스키마의 DB 를 열면 버전 오류가 난다.

### 단계 2 — 재실행에 붙이기, 업무 테이블로 전파

할 일
- `handlers/base.py`: 필드 행을 만든 뒤 호출하는 `apply_reviews(ctx, rows)` — `has_value_raw` 를 채우고, 유효한 검수로 `value_final`·`has_value`·`review_status`·`reviewed_by`·`reviewed_at` 을 덮는다.
- `handlers/haul.py`, `handlers/inspection.py`: 업무 행을 **최종 필드 행에서** 만들도록 고친다 (지금 `haul` 은 인식 결과에서 바로 `trips` 를 만든다). `trips_raw` 를 채운다. 4.4 의 상태 규칙을 적용한다.
- `FormHandler.on_review(con, field_id)`: 저장 직후 업무 테이블을 갱신하는 훅. 기본은 아무것도 하지 않는다. `haul` 은 `prod_haul`, `inspection` 은 `insp_daily`(점검내역).
- `validate/crosscheck.py`: 날짜를 지정해 다시 계산할 수 있게 한다. `xcheck_haul` 에 기계 값 기준 횟수(`log_trips_raw`, `matrix_trips_raw`)를 같이 적는다.
- `review/store.py`: `save(con, site, settings, review)` = 파일 추가 → `doc_review` → `doc_field` → `on_review` → 그 날짜의 교차검증.
- `pipeline/runner.py`: 시작할 때 검수 파일을 읽어 들인다. 문서 상태(`needs_review`) 계산은 그대로 `pending` 건수로.

수용 기준 (합성 데이터, `null` 백엔드)
- 하루치 운반 셀 전부를 정답대로 `save` 하면 그 날짜의 `xcheck_haul` 이 횟수까지 비교한 기대값과 같아진다 (`tools.synth.expected_xcheck([그날], with_trips=True)`).
- 4.4 의 불변식: 몇 건을 `save` 한 DB 와, 새 `WORK_ROOT` 에서 같은 검수 파일로 처음부터 돌린 DB 의 `build_report` 및 `prod_haul`·`insp_daily`·`xcheck_haul` 내용이 같다.
- 검수된 셀도 `value_raw`·`backend` 는 기계 값 그대로다. `oracle` 로 다시 돌리면 `value_raw` 는 오라클 값, `value_final` 은 검수값이다.
- `empty` 검수는 `has_value` 를 0 으로, `illegible` 은 `pending` 으로 둔다.
- 검수가 없을 때 기존 테스트가 전부 그대로 통과한다 (리포트의 기존 항목 불변).

### 단계 3 — 대기열

할 일: `review/queue.py`. 대기열은 "항목"의 목록이고, 항목은 셀 하나 또는 함께 봐야 하는 셀 묶음이다.

| 이름 | 내용 | 기계 값 표시 |
|---|---|---|
| `haul-numbers` | 운반 숫자 셀의 표본 | 숨김 |
| `mismatch` | 교차검증 불일치 칸마다 한 항목: 일보의 주간·야간 셀과 행렬 셀(여러 장이면 전부)을 묶는다 | 숨김 |
| `pending` | 검수 대기 필드 전부, 쪽 순서. `--template`, `--kind` 로 거른다 | 미리 채움 |

`haul-numbers` 의 표본 규칙
- 모집단: `prod_haul` 의 셀 중 검수가 없는 것.
- 값이 있다고 판단된 셀(`has_value_raw` = 1)에서 `n × (1 − empty_share)` 개, 비었다고 판단된 셀에서 `n × empty_share` 개 (기본 0.1). 빈 칸 표본이 있어야 "값을 놓친" 오류를 잴 수 있다.
- 날짜 × 양식 역할(일보/행렬)로 층을 나눠 고르게 뽑는다.
- 뽑는 순서는 `hash(seed, field_id)` 로 정한다. 검수가 진행되어도 표본의 구성이 바뀌지 않는다 (이미 한 것만 빠진다).
- 보여 주는 순서는 날짜 → 쪽 → 행 → 열. 같은 쪽의 셀이 이어져야 빠르다.

수용 기준
- 같은 `seed` 는 같은 표본. 몇 개를 검수한 뒤 다시 만들면 남은 것이 원래 표본의 부분집합이다.
- 빈 칸 비율이 지정한 값과 맞는다 (모집단이 모자라면 있는 만큼).
- `mismatch` 항목 수 = `xcheck_haul` 의 `mismatch` 행 수. 각 항목에 양쪽 문서의 셀이 다 들어 있다.
- 검수된 필드, `illegible` 로 표시된 필드는 어느 대기열에도 다시 나오지 않는다.

### 단계 4 — 화면

할 일
- `review/crops.py`: `cell_png(field_id, pad, scale)` 과 `row_png(field_id)` (그 행 전체를 자르고 대상 셀에 테두리 — 인쇄된 광종·편이 같이 보여야 한다). 정합 이미지가 없으면 무엇을 해야 하는지 말하는 오류.
- `review/server.py`, `review/static/index.html`:

  | 경로 | 내용 |
  |---|---|
  | `GET /` | 화면 |
  | `GET /api/queue?name=…&n=…&seed=…` | 항목 목록 (제목, 셀들의 `field_id`·라벨·기존 검수), 전체/완료 수 |
  | `GET /crop?field_id=…&kind=cell\|row` | PNG |
  | `POST /api/review` | `{field_id, verdict, value, note}` → `save`. 검수자는 서버를 띄울 때 정한다 |
  | `GET /api/stats` | 진행 현황 |

- 화면 동작
  - 위에 행 띠, 아래에 3배로 키운 셀. 제목에 날짜 · 양식 · 행(광종/편) · 열(주간/야간 또는 자리).
  - 입력창에 자동으로 초점. **Enter** = 저장하고 다음. **빈 채로 Enter** = 빈 칸. **`?` 후 Enter** = 읽을 수 없음. **PageUp/PageDown** = 이전/다음.
  - 숫자 셀은 숫자만 받는다. 세 자리 이상이면 한 번 더 확인한다.
  - 이전 항목으로 돌아가면 입력했던 값이 보이고, 다시 저장하면 새 검수가 추가된다.
  - 묶음 항목(`mismatch`)은 셀마다 입력창. Tab 으로 이동, 마지막 칸에서 Enter 하면 전부 저장.
  - 진행 수(완료/전체)와 검수자 이름을 늘 보여 준다. 4.5 의 "적힌 그대로" 안내를 화면에 한 줄 넣는다.
- `cli.py`: `review serve [--queue] [--n] [--seed] [--empty-share] [--template] [--kind] [--reviewer] [--port]`, `review stats`.

수용 기준 (서버를 포트 0 으로 스레드에 띄워 `urllib` 로 시험)
- 대기열을 받고, 크롭이 PNG 로 오고, `POST` 한 검수가 파일과 DB 에 들어가고, 대기열을 다시 받으면 그 항목이 빠져 있다.
- 숫자 셀에 글자를 보내면 거절한다(400). 없는 `field_id` 는 404.
- 서버 주소가 `127.0.0.1` 이다. `index.html` 에 외부 주소(`http://`, `https://`, `//cdn`)가 없다.
- `haul-numbers` 와 `mismatch` 의 응답에는 기계 값이 들어 있지 않다. `pending` 에는 들어 있다.
- 패키지를 설치한 상태(`pip install .`)에서도 `index.html` 을 찾는다.

### 단계 5 — 평가와 일치율

할 일
- `cli.py`: `review export-answers OUT.json` — 유효한 검수(`value`, `empty`)를 `answers.json` 형식으로. `illegible` 은 뺀다.
- `evaluate/fields.py`, `cli.py`: `eval --target raw|final` (기본 `final`). `raw` 는 `value_raw` 를 비교한다. 검수값과 비교할 때는 `raw` 를 쓴다
  (`final` 은 검수값 자신이라 언제나 맞는다).
- 정답이 빈 칸인 셀과 값이 있는 셀의 정확도를 따로 낸다. 빈 칸이 대부분이라 합치면 인식기의 성적이 가려진다.
- 값 유무 판단의 성적: 검수가 있는 셀에서 `has_value_raw` 대 검수의 `value`/`empty` → 정밀도·재현율. `eval` 결과에 넣는다.
- `report.py`: 교차검증 일치율 — 양쪽 다 값이 있는 칸 중 횟수가 같은 비율을, 기계 값 기준과 최종 값 기준으로 따로. 분모(칸 수)를 같이 낸다.
- `review stats`: 판정별·양식별·날짜별 건수, 검수자별 건수, `bbox` 가 달라진 기록 수.

수용 기준 (합성 데이터)
- 정답대로 전부 검수한 뒤: `oracle` 로 돌리면 `eval --target raw` 정확도 1.0, `null` 로 돌리면 정답에 값이 있는 숫자 셀의 정확도 0.0 (빈 칸 셀은 1.0). 두 경우 모두 값 유무의 정밀도·재현율은 1.0.
- 기계 값 기준 일치율: `oracle` 에서 (양쪽 값 있는 칸 − 횟수가 다른 칸) / 양쪽 값 있는 칸 — `truth` 에서 계산한 값과 같다. `null` 에서는 분모가 0 이고 비율은 `null` 로 낸다.
- 리포트에 항목이 추가되므로 실데이터 회귀 기준과 "항목 없음" 차이가 난다. **기존 항목의 값이 하나도 바뀌지 않았음을 확인한 뒤** `regress --update` 한다.

여기까지 끝나면 PR 을 연다.

### 단계 6 — 페이지 필드 검수 (수동 라벨 대체)

배경: 차량별 일보의 차량번호·작성자는 지금 사람이 `labels/pages.json` 에 손으로 적는다. 묶음 PDF 전체(약 150일)를 교차검증하려면
일보 쪽마다 이 두 값이 필요하다. 같은 화면으로 입력하게 한다.

할 일
- 템플릿 `fields[]` 에 선택 항목 `meta_key` 추가 (예: `meta_key: vehicle_no`). `forms/template.py` 검증, `docs/SITE_PACK.md`, 합성 템플릿에 반영.
- `pipeline/runner.py`: 페이지 메타의 우선순위를 **검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙** 으로.
- 대기열 `page-fields`: `meta_key` 가 있는 필드 중 검수가 없는 것. 후보 목록 = 행렬 템플릿 머리글의 값 + 지금까지 검수·라벨에 나온 값. 숫자 키(1–9)로 고르거나 직접 입력.
- 저장하면 그 쪽의 `prod_haul` 차량·작성자와 그 날짜의 교차검증이 갱신된다.

수용 기준 (합성 데이터)
- `labels/pages.json` 을 비우고 같은 값을 검수로 넣으면 `xcheck_haul` 과 `eq_assignment_obs` 가 라벨이 있을 때와 같다.
- 라벨과 검수가 다르면 검수가 이긴다.

## 6. 시험

- 전부 합성 데이터로 한다 (`tests/conftest.py` 의 `synth` 픽스처). 실제 문서의 이미지나 값을 테스트에 넣지 않는다.
- 검수 파일은 사이트 팩 안에 생긴다. 세션 범위 픽스처의 사이트 팩을 더럽히지 않도록, 검수를 쓰는 테스트는 `reviews` 경로를 `tmp_path` 로 돌린다.
- 새 테스트 파일: `tests/test_review_store.py`, `test_review_queue.py`, `test_review_server.py`. 4.4 의 불변식 테스트는 `test_review_store.py` 에.
- 전체 시험 시간이 지금(약 1분)의 두 배를 넘지 않게 한다. 파이프라인을 새로 돌리는 테스트는 하루치(`days=1`)로.

## 7. 실데이터에서 확인할 것 (데이터가 있는 컴퓨터에서)

1. `minedocscan regress` — 기존 항목 불변 확인 후 `--update`.
2. 3일치 묶음으로 화면을 띄워 20셀쯤 입력해 본다: 셀당 걸린 시간, 행 띠만으로 어느 칸인지 알 수 있는지, 불편한 점.
3. 묶음 PDF 전체를 돌릴 때의 시간과 `aligned/` 용량을 재서 보고한다 (지금은 쪽당 약 1초, 쪽당 약 0.5 MB).

## 8. 지켜야 할 것

- `reviews.jsonl` 은 사이트 팩에 있다. 저장소에 넣지 않는다. 실제 값이 찍힌 화면 갈무리를 문서·PR·이슈에 붙이지 않는다.
- 서버 로그에 입력값과 이미지 내용을 찍지 않는다. 요청 경로와 상태 코드만.
- 판정 규칙의 임계값을 건드리지 않는다. 건드려야 한다면 이 작업과 분리한다.
- `CLAUDE.md` 의 작업 규칙(수치로 판단, `upsert` 만 사용, 한글 경로, 주석은 한국어)을 따른다.

## 9. 열린 질문과 기본값

| 질문 | 기본값 |
|---|---|
| 검수자 이름은 어떻게 적는가 | `--reviewer` 로 받은 짧은 영문 식별자. 없으면 서버를 띄우지 않는다 |
| 검수 파일을 공유 드라이브의 사이트 팩에 두어도 되는가 (동기화 충돌) | 둔다. 한 번에 한 사람이 입력한다고 가정하고, 문서에 그렇게 적는다 |
| 첫 표본 크기 | 1,500셀, 빈 칸 10 % |
| `illegible` 을 나중에 다시 볼 방법 | `review serve --queue illegible` 은 만들지 않는다. `review stats` 에 건수만 |
| 포트 | 8765, 쓰이고 있으면 오류를 내고 `--port` 를 안내 |

## 10. 문서

- `docs/ARCHITECTURE.md`: 3절 상태(`reviewed`), 6절 데이터 모델(`doc_review`, `has_value_raw`, `trips_raw`), 검수 흐름 절 추가.
- `docs/SITE_PACK.md`: `reviews/`, `meta_key`.
- `docs/DATA.md`: 검수 파일의 위치와 백업, 검수값으로 평가하는 법.
- `docs/ROADMAP.md`: "지금 되는 것" 표와 M2 갱신.
- `docs/decisions/0008-…md`: 4.1(원본은 추가 전용 파일), 4.3(기계 값과 최종 값 분리), 4.5(눈가림 옮겨 적기)를 결정 기록으로.
- `CLAUDE.md`: 패키지 지도에 `review/`, 자주 쓰는 명령에 `review serve`.

## 11. 끝나고 보고할 것

- 단계별로 무엇을 만들었고 무엇을 만들지 않았는지. 수용 기준 중 충족하지 못한 것이 있으면 그것부터.
- 테스트 수와 시간, 회귀 결과(기존 항목 불변 여부).
- 4절에서 벗어난 것이 있으면 무엇을 왜.
- 실데이터에서 잰 것(7절)과 화면을 써 본 소감 — 고쳐야 할 것 위주로.
