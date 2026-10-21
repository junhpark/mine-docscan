# 작업 지시서 0013 — 2단계의 첫 화면: 통합 DB 를 읽는 대시보드 (새 저장소)

상태: 지시서
구현 저장소: **`junhpark/mine-dashboard`** (새 저장소 — mine-docscan 이 아니다. 이름은 9절)
관련 (mine-docscan — 새 저장소로 복사하면 상대 링크가 깨지므로 주소로 적는다, 아래 `D` = `https://github.com/junhpark/mine-docscan/blob/main/docs`):
`D/ROADMAP.md` "2단계와의 접점", `D/decisions/0021-publish-to-the-shared-db.md`(통합 DB 로 싣기),
`D/decisions/0022-exports-are-copies.md`(확정된 값만), `D/DATA.md` "통합 DB", `D/tasks/0008-export.md`(싣기·엑셀)

## 0. 이 문서를 쓰는 법

> `junhpark/mine-docscan` 의 `CLAUDE.md` 와 `docs/tasks/0013-stage2-dashboard.md` 를 읽고, 새 저장소 `junhpark/mine-dashboard` 에서
> 단계 1부터 4까지 이어서 진행해. 다 끝나면 그 저장소에 PR 을 하나 열고 결과를 보고해.

- **사람이 먼저** 빈 저장소 `junhpark/mine-dashboard` 를 만들고(README 한 줄로 `main` 이 있게) 세션에 붙인다 (8절 1). 구현 세션은 mine-docscan 을
  읽기만 한다 — 고치지 않는다. 1단계를 고쳐야 하는 일(계약 — 4.2)이 나오면 멈추고 묻는다.
- 새 저장소의 브랜치 `feat/dashboard`, PR 하나, 단계마다 커밋 하나 (단계마다 그 저장소의 `pytest`·`ruff check .` 통과).
  단계 1 의 첫 커밋에 이 지시서를 `docs/tasks/0001-dashboard.md` 로 복사해 넣는다 (그 저장소의 지시서 1번).
- 단계 사이에 멈추지 않는다. 다만 4절을 바꿔야 하거나 수용 기준을 충족할 수 없으면 거기서 멈추고 묻는다.
- 실데이터는 없다. 수용 기준은 전부 합성이다 — mine-docscan 판 1.1.0 의 `synth` 로 만든 묶음을 그 판으로 처리·실은 것.
- 1단계의 계약: 스키마 버전 8, 싣기의 판(`PUBLISH_VERSION`) 2, 판 1.1.0 — 커밋 `02344fb230f389074fd08063ee3a9823bacc8aa5` (태그 `v1.1.0` 이 생기면 그것).

## 1. 왜 하는가

**(가) 표로는 의미가 보이지 않는다.** 1단계는 스캔을 업무 표(운반·가동·점검·교차검증)로 만들고 엑셀과 통합 DB(PostgreSQL)에 사본을 싣는다. 사용자가
보고 싶은 것은 추이와 비교다 — 날마다 운반이 얼마인가, 일보와 행렬이 얼마나 맞는가, 장비가 몇 시간 돌았나, 검수가 얼마나 밀렸나.
1단계의 범위 규칙은 시각화를 2단계로 보낸다 ("2단계 기능을 여기에 넣지 않는다"). 2단계는 통합 DB 의 실은 표를 **읽기만** 한다 (ADR 0021).

**(나) 실은 표에는 확정되지 않은 값이 섞여 있다.** 통합 DB 의 표는 작업 DB 그대로라 검수 대기인 기계 값도 들어 있다 (엑셀과 다르다 — ADR 0022).
지금 실데이터에서는 인식기를 학습하기 전이라 운반 값의 거의 전부가 검수 대기다. 대시보드가 그 값을 그냥 더하면 **틀린 그래프**가 된다.
→ 이 대시보드의 첫째 규칙: **더하는 것은 확정된 값만, 확정되지 않은 것은 따로 센다** (4.3).

**(다) 기존 통합 DB 프로그램의 자리를 이어받는다.** 그 프로그램의 조회·통계 화면(월별 추이, 채광·선광·출하 통계)이 이 대시보드의 자리다.
DB 에 바로 붙는 2계층 클라이언트의 흔한 문제를 피한다 — 브라우저는 DB 에 붙지 않는다, 읽기 전용 계정, 자리표시자, 비밀값은 환경변수·pgpass,
처음에는 127.0.0.1 에만 (4.6). 자세한 것은 프로젝트 문서.

**(라) "스캔하면 바로 보이는" 시연.** 1단계의 `serve` 는 몇 초 안에 스캔을 처리한다. 대시보드가 같은 PC 의 작업 DB(시연 — 4.2)를 몇 초마다 다시 읽으면
스캔이 그래프에 들어오는 것이 보인다.

## 2. 끝났을 때 되는 일

```bash
pip install -e ".[dev]"
minedash demo ~/mdash                      # mine-docscan(판 1.1.0)의 명령으로 합성 묶음 → 처리 → 작업 DB ~/mdash/work/minedocscan.db (시연용)
minedash serve --sqlite ~/mdash/work/minedocscan.db     # 시연: 127.0.0.1:8770 — 화면 위에 "시연 — 작업 DB 를 직접 읽음"
MINEDASH_DB_URL=postgresql://reader@host/db minedash serve --schema minedocscan    # 운영: 통합 DB (비밀번호는 pgpass)
minedash check [--sqlite F | --schema S]   # 계약 확인: 스키마 버전·싣기의 판·사이트·마지막 싣기·권한 — 0 맞음 / 2 닿지 못함·맞지 않음·권한 없음
```

화면 (한국어, 망 없이 — 4.4):
1. **개요** — 기간의 핵심 수(처리한 쪽, 자동 적재율, 검수 대기 칸, 확정된 운반 횟수, 교차검증 일치율, 가동 시간)와 날마다의 작은 추이.
2. **운반** — 날마다 확정된 운반 횟수(광종·주야(shift)로 나눠 쌓은 막대, 편(level)으로 거르기), 미확정 행의 수(따로), 자리별 표.
3. **교차검증** — 날마다 일치·불일치·한쪽만의 수와 일치율(분모와 함께), 잠정(검수 대기 값이 낀 비교)과 자리 미정은 따로.
4. **장비 가동** — 장비 × 날짜의 가동 시간(근거별), 계기 검산의 어긋남 수.
5. **점검** — 장비 × 날짜의 이상 유무, 점검하지 않은 날.
6. **처리 현황** — 날마다 양식별 쪽 수와 상태, 칸의 상태(자동·검수 대기·검수함·판독 불가) — 검수가 얼마나 밀렸나.

모든 그래프에 "확정 n · 대기 m" 이 붙고(교차검증·계기 검산은 "잠정 m"), 대기는 합에 들어가지 않는다. 자동 새로 고침(기본 10초, 끌 수 있다).

## 3. 범위

하는 것
- 새 저장소의 뼈대(4.1), 데이터 소스와 계약 확인(4.2), 지표(4.3), 화면(4.4), API(4.5), 서버(4.6), 시연 명령(4.7), 시험과 CI(6절), 문서(10절).

하지 않는 것
- **DB 에 쓰는 것 전부** — 통합 DB 의 실은 표에도, 자기 표에도 (2단계의 직접 입력·엑셀 올리기는 다음 지시서). 읽기 전용 트랜잭션만.
- 이름·차량번호·등록번호를 화면·API·로그에 내는 것 (4.3 라). 원래 파일명·보관 경로·검수자도.
- 로그인·여러 사용자·LAN 에 여는 것 (127.0.0.1 에만 — 9절), 윈도우 오프라인 설치 묶음 (다음 지시서 — 9절).
- 조정(reconciliation)·물질수지 계산·가동 시간의 분류(가동·대기·정비) — 보여 주기만 (ADR 0006).
- 기존 통합 DB 프로그램의 자료(벨트스케일·계근·경유·화약 엑셀)와 그 화면.
- mine-docscan 을 고치는 것.

## 4. 설계

### 4.1 저장소와 꾸러미

- `junhpark/mine-dashboard`, 꾸러미 `minedash`, 명령 `minedash`. Python 3.11+, `ruff`(줄 길이 110), 주석·문서는 한국어·식별자는 영어 (mine-docscan 과 같다).
- 런타임 의존성: `psycopg[binary]`(PostgreSQL — LGPL-3.0, 묶음을 만들 때 소스를 같이 — mine-docscan 과 같은 처리), 나머지는 표준 라이브러리.
  HTTP 서버는 표준 라이브러리(`ThreadingHTTPServer`) — 읽기 전용 JSON 과 정적 파일뿐이라 프레임워크를 들이지 않는다 (이 저장소의 ADR 0003).
- 그래프: **Apache ECharts**(Apache-2.0)의 `echarts.min.js` 를 저장소에 담는다 (`src/minedash/static/vendor/` + `LICENSE`·`NOTICE`, 판을 고정하고
  SHA-256 을 시험이 본다). CDN·웹 글꼴·외부 주소를 쓰지 않는다 — 현장은 외부망이 없다.
- 개발 의존성 `[dev]`: pytest, ruff, 그리고 시험의 합성 데이터를 만드는 **mine-docscan 판 1.1.0**
  (`minedocscan[postgres] @ git+https://github.com/junhpark/mine-docscan@02344fb230f389074fd08063ee3a9823bacc8aa5`). 런타임은 mine-docscan 을
  import 하지 않는다 (시연 명령은 그 **명령**을 부른다 — 4.7).
- `CLAUDE.md`(한국어 — 범위, 원칙, 개인정보 규칙은 mine-docscan 의 것을 이어받아 이 저장소에 맞게), `README.md`, `docs/`(10절).

### 4.2 데이터 소스와 계약

소스는 둘이고 같은 쿼리를 쓴다 (쿼리는 `minedash/queries.py` 한 곳):

| 소스 | 쓰임 | 여는 법 |
|---|---|---|
| **PostgreSQL** — 통합 DB 의 실은 스키마 | 운영 | URL 은 환경변수 `MINEDASH_DB_URL` 로만(설정 파일에 적으면 오류), 비밀번호는 pgpass. 스키마는 `--schema`(기본 `minedocscan`). 요청마다 `READ ONLY`·`REPEATABLE READ` 트랜잭션 하나, 짧은 `statement_timeout`(기본 5초 — 열린 읽기 트랜잭션은 1단계 `publish --rebuild` 의 `DROP` 을 그쪽의 `lock_timeout` 까지 기다리게 한다)·`lock_timeout`, 연결 시간 제한. URL·비밀번호는 어디에도 찍지 않는다 — 호스트·DB·스키마만 |
| **SQLite** — mine-docscan 의 작업 DB 파일 | **시연·시험 전용** | `file:…?mode=ro` 로 읽기만, **요청마다 열고 닫는다**(윈도우에서 파일을 쥐고 있으면 1단계의 `run --fresh` 가 "다른 프로그램이 쓰고 있습니다"로 멈춘다). 화면 맨 위에 "시연 — 작업 DB 를 직접 읽음" 띠 |

- **SQLite 소스는 1단계 ADR 0021 의 예외다.** 그 ADR 은 다른 프로그램이 작업 DB 를 직접 읽지 않는다고 정했다 (현장 PC 에서 지우고 다시 만드는 파일).
  이 저장소의 ADR 0001 에 "운영은 통합 DB 만, 작업 DB 는 시연·시험에서만 — 읽기 전용, 요청마다 열고 닫는다"를 적는다. 운영 설정(설정 파일)에는 SQLite 를 둘 수 없다.
- **같은 쿼리**: 두 DB 에서 같은 값이 나오게 — 집계는 `COUNT(*)`·`COUNT(조건이 맞는 열)` 이나 파이썬에서, `SUM(비교식)` 을 쓰지 않는다 (1단계에서 배운 것),
  `SUM` 은 정수 열에만(PostgreSQL 에서 `BIGINT` 의 합은 `NUMERIC` → `Decimal` 로 온다 — 응답을 만들 때 정수·실수로 고친다), **정렬은 파이썬에서**
  (PostgreSQL 의 정렬 규칙(ICU·en_US)과 SQLite 의 글자 순서가 다르다), 날짜는 ISO 글자의 비교만.
- **계약**: 대시보드가 읽는 표·열과 **API 가 내보낼 수 있는 열(허용 목록)**을 `minedash/contract.py` 한 곳에 적는다 — 실은 표 12개 중 쓰는 것과 그 열,
  상태 표(`pub_meta`·`pub_state`). 1단계의 `PUBLISH_SKIP_COLUMNS` 다섯(`created_at`·`received_at`·`work_requested`·`work_done`·`source_path`)은 쓰지 않는다.
- **판 확인**: PostgreSQL 은 `pub_meta` 의 `schema_version` ∈ {8}, `publish_version` ∈ {2}, `site` 를 읽는다. SQLite 는 `meta_schema` 의 `schema_version` ∈ {8}.
  받지 않는 판이면 화면을 열지 않고 한 줄(받는 판과 찾은 판)로 멈춘다 (`check` 는 종료 코드 2). `pub_meta` 가 없으면 "아직 실은 것이 없음",
  스키마가 없거나 읽을 권한이 없으면(누가 스키마를 지우면 권한도 사라진다) "스키마가 없거나 권한이 없음" — 예외의 종류만, 드라이버의 글 없이.
- **마지막 싣기**: `pub_state.published_at` 의 최댓값을 화면에. 1단계의 처음 싣기는 달마다 조각으로 가므로 다 돌 때까지 앞의 달만 있을 수 있다 —
  자료가 있는 날짜의 범위를 같이 보인다.
- **계약 시험**: `contract.py` 의 표·열이 mine-docscan 판 1.1.0 의 `schema.sql`·`PUBLISH_TABLES`·`PUBLISH_SKIP_COLUMNS` 에 있다 (개발 의존성으로 읽는다).

### 4.3 지표의 규칙 — 가장 중요하다

`docs/METRICS.md` 에 표 하나로 적고, 시험이 그 표대로 본다.

**가. 상태는 DB 만으로 정한다.** 통합 DB 에는 사이트 팩(템플릿)이 없다 — 1단계 엑셀(`export/business.py`)의 상태 중 템플릿을 읽는 것(근무 시각 칸의
역할, 점검표의 ✓ 두 칸과 핸들러가 고쳐 말한 상태, 계기 검산의 소계 짝)은 그대로 옮길 수 없다. 그래서:

| 표 | 확정 | 대기 | 판독 불가 |
|---|---|---|---|
| `prod_haul`·`prod_tally` | 행의 `review_status` 가 `auto`·`reviewed` | `pending` | `pending` 이고 출처 칸(`source_field_id` 의 `doc_field`)에 `reviewed_by` 가 있다 |
| `eq_usage_daily` (가동 시간) | `review_status` 가 `auto`·`reviewed` **이고** `hours` 가 있다 | `pending` (형식 칸이 검수 대기면 `value_final` 이 NULL — `hours` 도 NULL) | 출처 칸(`start_field_id`·`end_field_id`·`total_field_id`) 중 하나가 판독 불가 |
| `insp_daily` | 행의 `review_status` 가 `auto`·`reviewed` (ADR 0022: 읽는 쪽이 `review_status` 로 거른다) | `pending` | — |
| `xcheck_haul` | 비교한 일보·행렬의 `prod_haul` 행이 모두 확정 | 하나라도 대기 — **잠정** | — |
| `xcheck_usage` | `field_a`·`field_b` 의 칸이 모두 확정 | 하나라도 대기 — **잠정** | — |

- `xcheck_haul` 의 짝짓기는 1단계 `export/business.py` 의 `xcheck_haul_sheet`·`_xcheck_rows_of`·`unresolved_pages` 를 읽고 같은 열로 (날짜·자리·광종·편 — 주야가 없다).
  짝이 되는 운반 행이 없으면 잠정으로 센다.
- 시험: 합성 작업 DB 에서 대시보드의 상태를 1단계 엑셀의 업무 시트(`export/business.py` 의 상태 열)와 견준다. 템플릿을 읽어서 다른 행(위의 셋)은
  `METRICS.md` 에 "엑셀과 다른 경우"로 적고, 시험은 그 밖의 행이 모두 같은지 본다.

**나. 더하기.**
- 합·평균·비율의 분자는 **확정된 행만**. 대기·판독 불가는 같은 그래프에 따로(수로) — 합에 넣지 않는다. 확정되지 않은 행이 하나라도 있는 날짜의 합은
  "잠정" 표시.
- **문서 없음 ≠ 0**: 그날 그 양식의 쪽이 없으면 점을 찍지 않는다 (빈 칸). 쪽이 있고 값이 0 이면 0.
- 운반(`prod_haul`): 일보(`source_role = 'log'`)와 행렬(`matrix`)은 같은 운반을 두 번 적은 것이다 — **더하지 않는다**. 날마다의 운반 = 일보의 확정된 횟수
  (`has_value = 1` 의 `trips`)의 합 — 광종(`material`)마다 따로, 주야(`shift`)로 쌓고, 편(`level`)으로 거른다. 행렬에 없는 갱외 행(합성의 `SURFACE`)도
  일보의 광종 하나로 보인다 (교차검증에는 없다). 행렬은 교차검증 화면에서만.
- 교차검증(`xcheck_haul`): `status` 별 수(`match`·`mismatch`·`missing_log`·`missing_matrix`), 일치율 = match / (match + mismatch) — 분모를 같이.
  잠정과 **자리 미정**(`slot` 이 `unresolved:` 로 시작 — 일보에 자리가 없다)은 따로 센다.
- 가동(`eq_usage_daily`): 쪽 하나에 한 행이다 — 한 장비가 하루 두 장이면 두 행. 장비·날짜의 가동 시간은 **같은 근거(`hours_basis`)끼리만** 더한다.
  근거가 섞인 장비·날짜는 "근거 섞임"으로 보이고 더하지 않는다. `equipment_id` 가 없는 행(대응표에 없는 이름)은 "대응표에 없음 n" 으로 센다.
  계기 검산(`xcheck_usage`)의 `result`(`match`·`gap`·`overlap` …) 수의 추이 — 고치지 않고 보여 준다.
- 작업량(`prod_tally`): 소계 칸(`is_subtotal = 1`)은 뺀다.
- 점검(`insp_daily`): `abnormal` 1 = 이상, 0 = 정상, NULL = 판정 불가. 그날 점검표가 없으면 빈 칸.
- 처리 현황: `doc_page.status` 별 쪽 수(날짜·양식), `doc_field.review_status` 별 칸 수(종류별). **자동 적재율은 1단계 리포트와 같은 정의** —
  손글씨 칸(`kind` 가 `handwritten…`) 중 `status_raw = 'auto'` 의 몫 (mine-docscan `report.py` 의 `_by_backend`), 분모를 같이.
  날짜 없는 것은 **문서**(`doc_document.status = 'needs_date'`)의 수로.

**다. 날짜.** 모든 축은 `work_date`(ISO 글자). 기간 필터의 기본은 자료가 있는 마지막 날부터 30일.

**라. 이름·번호를 내지 않는다 — 허용 목록.** API 는 `contract.py` 의 허용 목록에 있는 열(과 그것에서 센 수)만 보낸다. 특히 내지 않는 것:
- 사람·차: `operator`·`vehicle_no`·`header_operator`·`header_vehicle_no`(운반·배차), `doc_page_meta` 의 `value`·`machine_value`, `doc_field` 의
  `value_raw`·`value_final`·`candidates`, `reviewed_by`, `source_name`·`source_rel`, 오류·경고의 글(`error`·`warning`).
- **자리(`slot`)의 값** — 실제 사이트 팩에서 자리 이름은 차량번호다. 화면에는 "자리 1 … n"(자리 값을 정렬한 순서의 번호 — 기간과 상관없이 대상 전체에서
  정한다)으로, 자리 미정은 `unresolved:` 뒤의 글자 없이 "자리 미정 k" 로.
- 장비: `registration`·`equipment_key`·`hid`(합성에서는 등록번호가 들어 있다), 손으로 적은 장비명(`eq_usage_daily.equipment`·`prod_tally.equipment`·
  `xcheck_usage.equipment_ref`). 장비의 표시 = `eq_equipment` 의 `model`(없으면 `iso_type`) — 같은 것이 여럿이면 뒤에 #1, #2 (`equipment_id` 순).
  `equipment_id` 의 글자 자체도 화면에 그리지 않는다.
- 시험: 합성 DB 에서 위 열들의 값을 모아 어떤 API 응답·HTML 에도 없는지 본다 (6절).

### 4.4 화면

- 페이지 여섯 (2절), 위에 기간·양식 필터와 마지막 싣기·소스의 띠. 정적 HTML + JS 하나 + ECharts — 빌드 도구 없이.
- 막대(쌓은 막대 — 광종·주야), 선(비율·추이), 장비 × 날짜의 열지도(가동·점검), 핵심 수 타일. 확정은 진한 색, 대기·잠정·판독 불가는 회색·빗금 + 수.
  색에만 기대지 않는다 (범례·글자).
- 한국어 표시는 `minedash/labels.py` 한 곳 (광종·편·주야·상태·근거 …) — 1단계 `export/labels.py` 의 말을 따른다 (`level` = 편, `shift` = 주야).
  모르는 코드 값은 그대로 보인다. 사이트마다 다른 이름은 설정 파일(`[labels]`)로 덮을 수 있다 (사람·차의 이름은 거기에도 두지 않는다).
- 자동 새로 고침: 기본 10초, 끄기. 같은 데이터면 그래프를 다시 그리지 않는다 (응답의 해시).
- 빈 상태: "이 기간에 자료가 없음", "아직 실은 것이 없음", "받지 않는 판", "스키마가 없거나 권한이 없음" — 각각 다른 문장.
- 화면 너비 1280 과 390(휴대폰)에서 깨지지 않는다.

### 4.5 API

- `GET /api/<페이지>?from=YYYY-MM-DD&to=…&form=…` → JSON (수와 허용 목록의 코드 값만). 응답 = 그 순간 DB 의 한 시점(4.2 의 트랜잭션 하나)에서 낸 것.
- 같은 DB·같은 인자면 같은 바이트 (키 순서·정렬 고정, 정수는 정수·실수는 정해진 자릿수로) — 시험이 소스 둘을 견준다 (`/api/meta` 는 소스마다 다르므로 뺀다).
- `GET /api/meta` — 소스의 종류(시연·운영), 판, 사이트, 마지막 싣기, 자료가 있는 날짜의 범위.

### 4.6 서버

- `127.0.0.1` 에만, 기본 포트 8770 (1단계 `serve` 의 8765 와 겹치지 않게). 포트를 혼자 쓴다 (윈도우는 `SO_EXCLUSIVEADDRUSE` — 1단계에서 배운 것).
- 표준 출력·오류는 UTF-8 로 다시 연다 (한국어 윈도우의 cp949 — 1단계의 `utf8_streams` 와 같은 일).
- 로그: 요청의 경로·상태·시간만 (질의 글자·값·URL 없이). DB 오류는 예외의 종류만 (드라이버의 글은 싣지 않는다 — 1단계와 같다).
- GET 만 받는다. 정적 파일은 꾸러미 안의 것만.

### 4.7 시연 명령 `minedash demo OUT`

- PATH 의 `minedocscan`(또는 `--minedocscan 경로` — 윈도우 설치 묶음의 것도)을 불러 합성 사이트 팩·묶음을 만들고(`synth OUT --days N --usage-logs --display-names`)
  작업 DB **`OUT/work/minedocscan.db`** 하나를 만든다. 확정된 값과 검수 대기가 둘 다 있게:
  - 앞의 날짜는 정답 oracle(`run <그 날짜의 파일> --answers …`)로, 뒤의 날짜는 기본 `null` 로 — `run <파일들>` 은 `--fresh` 없이 날짜를 나눠 돌릴 수 있다.
  - oracle 로 돌릴 때의 설정에 `[recognize.by_format]`(소수·시각 형식 넷)을 켠다 — 없으면 형식 칸이 읽히지 않아 가동 기록이 전부 대기다.
  - 장비 가동 화면이 비지 않게 합성 사이트 팩의 `labels/pages.json` 에 합성 정답(`truth.json`)의 장비명을 쪽 라벨로 적는다 (`equipment` 메타 —
    합성이라 괜찮다. 실제 사이트 팩에서는 사람의 일). 대응표가 그 이름을 장비 마스터와 잇는지 보고, 안 이으면 합성 사이트 팩의 `[equipment.aliases]` 에 더한다.
  - 설정 파일은 `OUT` 안에 두고 `MINEDOCSCAN_CONFIG` 로 그 명령에만 준다 (사용자의 1단계 설정을 건드리지 않는다).
- 수용 기준(행으로): 운반·가동 각각에 확정과 대기가 **둘 다** 있는 날짜가 있다, 가동 화면에 장비가 둘 이상 보인다, 교차검증에 일치·불일치·잠정이 다 있다.
  합성 가동 일보의 "하루 두 장"이 1단계의 다시 스캔 판정(`dup_min_sim` 0.80)에 붙잡힐 수 있다 (0.801·0.803 을 봤다) — 그렇게 되면 그대로 두고 보고한다
  (1단계를 고치지 않는다).
- 마지막에 띄울 명령을 한 줄로 알린다 (`minedash serve --sqlite OUT/work/minedocscan.db`).
- README 에 "스캔하면 바로 보이는" 시연: 1단계의 합성 접수 시연(`synth --intake` + `serve`)의 작업 DB 를 `--sqlite` 로 가리키고 자동 새로 고침을 켜면
  접수 폴더에 파일을 넣을 때 그래프가 따라온다 — 시연이고 운영은 통합 DB 라는 것을 같이.

## 5. 단계

### 단계 1 — 저장소·계약·소스

할 일: 4.1, 4.2, `minedash check`, 4.7, CI 의 뼈대(6절).

수용 기준
- `pip install -e ".[dev]"` → `pytest` → `ruff check .` 통과. 이 지시서가 `docs/tasks/0001-dashboard.md` 에 있다.
- 계약 시험(4.2) 통과. 받지 않는 판(가짜 `pub_meta`·`meta_schema`)이면 `check` 종료 코드 2, 한 줄, URL 없이.
- 같은 합성 작업 DB 를 SQLite 로 읽은 것과, 그것을 mine-docscan `publish` 로 PostgreSQL 시험 스키마에 실어 읽은 것의 행 수가 계약의 표마다 같다 (`-m postgres`).
- `minedash demo` 가 4.7 의 기준을 지킨다 (시간을 보고).
- 읽기 전용 (`-m postgres`): 10절의 SQL 로 만든 계정은 `SELECT` 만 가진다 (쓰기는 거절). mine-docscan `publish --rebuild` 로 표를 다시 만든 뒤에도 그 계정이
  읽는다 (`--rebuild` 는 표만 지우고 스키마는 그대로 — `USAGE` 와 `ALTER DEFAULT PRIVILEGES` 가 남는다). 시험 계정·스키마는 시험마다 고유한 이름으로 만들고
  지운다 — 시험의 접속 계정에 `CREATEROLE` 이 없으면 그 시험만 건너뛴다.

### 단계 2 — 지표와 API

할 일: 4.3, 4.5.

수용 기준
- 4.3 가: 합성 작업 DB 의 업무 행에서 대시보드의 상태 = 1단계 엑셀 업무 시트의 상태 (`METRICS.md` 에 적은 다른 경우는 빼고 — 그 수를 보고).
- 페이지마다 API 의 수를 **시험이 따로 계산한 값**과 견준다 — 같은 작업 DB 를 시험 안의 별도 SQL·파이썬으로 (대시보드의 코드를 쓰지 않고). 적어도:
  날마다 확정된 운반 횟수(광종·주야), 미확정 행 수, 교차검증의 status 별 수·일치율·잠정·자리 미정 수, 장비·날짜의 가동 시간(근거별)·근거 섞임,
  소계를 뺀 작업량, 점검의 이상 수, 처리 현황의 상태별 수·자동 적재율(1단계 리포트의 값과도 같다).
- oracle 로만 돌린 날짜에서 날마다 확정된 운반 횟수(일보) = 합성 정답(`truth.json`)에서 바로 계산한 일보의 합.
- 4.3 나의 경우를 작게 만든 DB 로 한 줄씩: 문서 없는 날(점 없음) vs 0, 근거 섞임, 대응표에 없음, 소계, 판독 불가, 잠정·자리 미정인 교차검증.
- SQLite 와 PostgreSQL 의 API 응답이 바이트까지 같다 (`/api/meta` 를 뺀 모든 페이지, 기간 셋) — `-m postgres`.
- 이름·번호: 4.3 라의 열에서 모은 합성 값이 어떤 API 응답에도 없다.

### 단계 3 — 화면과 서버

할 일: 4.4, 4.6, `minedash serve`.

수용 기준
- 페이지 여섯이 합성 시연 DB 로 뜬다 — HTTP 시험(포트 0, 시계 주입, 잠들지 않음): 상태 200, 화면이 부르는 API 가 다 있다.
- **망 없이**: HTML·JS·CSS 에 `http://`·`https://` 주소가 없다 (상대 경로만), 담은 ECharts 의 SHA-256 이 고정한 값과 같다.
- 이름·번호: 4.3 라의 합성 값이 HTML 과 API 응답 어디에도 없다.
- 자동 새로 고침: 시험에서 DB 에 쪽을 더하면(합성 묶음의 하루를 더 처리) 다음 요청의 응답이 바뀐다 — 서버를 다시 띄우지 않고.
  그동안 1단계의 `run --fresh` 가 같은 DB 파일에서 멈추지 않는다 (요청마다 닫는다 — 윈도우 CI 에서).
- 로그에 질의 글자·값이 없다 (시험이 로그를 읽는다). GET 밖의 방법은 405.
- 브라우저로 한 번 본다 (Playwright 가 있으면 1280·390 너비의 갈무리를 세션 안에서 보고 깨진 곳을 고친다 — 합성이지만 PR 에 붙이지 않아도 된다).

### 단계 4 — 문서·CI·보고

할 일: 6절의 CI, 10절의 문서.

수용 기준
- CI 초록: `test`(3.11·3.12), `postgres`(**`ubuntu-24.04` 에 깔린 PostgreSQL 16** 을 켠다 — 서비스 컨테이너를 쓰지 않는다: mine-docscan PR #23 에서 도커 이미지
  내려받기가 시험 전에 실패했다), `windows`(기본 시험 — 포트·출력의 글자·경로·파일 잠금).
- README 의 명령을 새 환경에서 그대로 돌려 본다 (시연까지).

## 6. 시험

- 전부 합성 — mine-docscan 판 1.1.0 의 `synth` 와 `run`·`publish` 로 만든다 (시험의 준비가 그 **명령**을 부르거나 개발 의존성으로 함수를 부른다).
  합성 묶음은 세션 하나에 한 번 만들어 시험들이 나눠 쓴다 (`pytest` 의 session fixture — 시간을 보고).
- 표시: `postgres`(`MINEDASH_TEST_PG_URL` 이 없으면 건너뜀), `slow`(시연 명령 전체 등). 기본 `pytest` 는 3분 안.
- 실데이터·실제 그림을 저장소·시험 자료에 넣지 않는다. 갈무리는 합성만.

## 7. 지켜야 할 것

- **읽기만 한다.** 통합 DB 의 실은 표는 1단계가 갈아 끼운다 — 거기에도, 다른 곳에도 쓰지 않는다 (ADR 0021).
- **확정되지 않은 값을 확정된 것처럼 더하지 않는다** (4.3 — ADR 0022 의 원칙을 화면에).
- **고치지 않고 보여 준다** — 불일치를 맞추지 않는다 (ADR 0006).
- 이름·차량번호·등록번호·자리 값·파일명·검수자를 화면·API·로그에 내지 않는다 (4.3 라 — 허용 목록). 통합 DB 의 URL·비밀번호를 어디에도 찍지 않는다.
- 망을 쓰지 않는다 — CDN·웹 글꼴·외부 주소 없음. 의존성의 라이선스를 README 에 (ECharts Apache-2.0, psycopg LGPL-3.0).
- mine-docscan 을 고치지 않는다 — 계약이 모자라면 멈추고 묻는다.
- mine-docscan 의 `CLAUDE.md` 의 개인정보 규칙을 이 저장소의 `CLAUDE.md` 로 옮겨 지킨다. 두 저장소 모두 공개다 — 문서·커밋에 현장 이름·운영 서버의 주소나
  구성을 쓰지 않는다.

## 8. 사람이 할 일

PR 전:
1. GitHub 에 빈 저장소 `junhpark/mine-dashboard`(공개 범위는 mine-docscan 과 같게 — 코드만 있고 자료는 없다)를 만들고 README 한 줄로 `main` 을 만든다.
   구현 세션에 그 저장소를 붙인다.

PR 이 합쳐진 뒤:
2. 자기 PC 에서 시연: 파이썬 3.11+ 와 git 이 있으면 `pip install git+https://github.com/junhpark/mine-dashboard` → `minedash demo D:\mdash`
   (1단계 묶음으로 설치한 `minedocscan` 이 PATH 에 있거나 `--minedocscan`) → `minedash serve --sqlite D:\mdash\work\minedocscan.db`.
3. 통합 DB 서버가 정해지면: 1단계의 싣는 계정과 **따로** 읽기 전용 계정을 만든다 (10절의 SQL — 관리자가, 스키마를 만든 뒤), 대시보드를 돌릴 PC 에
   `MINEDASH_DB_URL`·pgpass.
4. 다음 지시서를 정한다: LAN 에 열기와 로그인, 윈도우 설치 묶음, 2단계의 직접 입력·엑셀 올리기(기존 프로그램의 자료).

## 9. 열린 질문과 기본값

| 질문 | 기본값 |
|---|---|
| 저장소·꾸러미 이름 | `junhpark/mine-dashboard`, `minedash` — 2단계의 다른 기능(직접 입력 …)도 이 저장소에 더해 가면 이름을 바꿀 수 있다 |
| 서버 | 표준 라이브러리 `ThreadingHTTPServer` — 로그인·쓰기가 들어올 때 다시 정한다 |
| 그래프 | Apache ECharts (담아서) |
| 어디서 열리나 | 127.0.0.1 만 — LAN·로그인은 다음 지시서 |
| 시연 소스 | 작업 DB(SQLite)를 읽기 전용으로, 요청마다 열고 닫는다 — 시연·시험 전용, 화면에 띠. 운영은 통합 DB 만 |
| 포트 | 8770 |
| 새로 고침 | 10초, 끌 수 있다 |
| 기본 기간 | 자료가 있는 마지막 날부터 30일 |
| 이름·번호·자리 값 | 내지 않는다 (설정으로 켜는 것도 만들지 않는다). 자리는 번호로, 장비는 모델·형식 + 번호로 |
| 받는 판 | 스키마 8, 싣기의 판 2 — 1단계가 판을 올리면 이 목록을 넓히는 PR |
| 윈도우 묶음 | 다음 지시서 (mine-docscan 의 `scripts/bundle.py` 방식을 따른다) |
| 라이선스 문구 | mine-docscan 과 같은 자리표시 (`Proprietary`) — 기관이 정한다 |

## 10. 문서 (새 저장소)

- `README.md`: 무엇인가(2단계의 첫 화면, 통합 DB 를 읽기만), 설치·시연·운영 명령, 화면 여섯, 확정만 더한다는 것, 의존성과 라이선스.
- `CLAUDE.md`: 범위·원칙·개인정보 규칙·자주 쓰는 명령·꾸러미 지도 (mine-docscan 의 꼴).
- `docs/METRICS.md`: 4.3 의 표 — 지표마다 어느 표·열, 확정의 뜻, 무엇을 빼는가, 분모, 1단계 엑셀과 다른 경우.
- `docs/DATA.md`: 소스 둘, 계약(판), 허용 목록, 무엇이 찍히지 않는가, 읽기 전용 계정의 SQL — 1단계의 스키마가 생긴 뒤(`CREATE SCHEMA … AUTHORIZATION <싣는 계정>`),
  그 계정 또는 관리자로:
  `CREATE ROLE dash_reader LOGIN; GRANT USAGE ON SCHEMA minedocscan TO dash_reader; GRANT SELECT ON ALL TABLES IN SCHEMA minedocscan TO dash_reader;
  ALTER DEFAULT PRIVILEGES FOR ROLE <싣는 계정> IN SCHEMA minedocscan GRANT SELECT ON TABLES TO dash_reader;` (이름은 예시). 스키마를 지우고 다시 만들면 다시 준다.
- `docs/decisions/`: 0001 통합 DB 를 읽기만 하고 작업 DB 는 시연·시험에만(1단계 ADR 0021 의 예외), 0002 확정만 더한다(DB 만으로 정하는 상태 — 1단계 엑셀과 다른 경우),
  0003 표준 라이브러리 서버 + 담은 ECharts.
- `docs/tasks/0001-dashboard.md` (이 지시서), 끝나면 상태와 12절.
- mine-docscan 쪽: 고치지 않는다. 이 지시서의 상태 줄은 사람이(또는 다음 문서 PR 이) "완료 (mine-dashboard PR #N)"로 바꾼다.

## 11. 끝나고 보고할 것

- 충족하지 못한 수용 기준부터.
- 시험 수와 시간 (기본 / `postgres` / `slow` / 윈도우), CI 의 작업들.
- 시연 묶음의 크기(날짜·쪽·칸)와 확정·대기·잠정의 수, `demo` 의 시간, 다시 스캔으로 붙잡힌 쪽이 있었는지.
- 페이지마다 API 응답 시간 (시연 DB, SQLite·PostgreSQL), 자동 새로 고침의 부하.
- 4.3 의 규칙 중 1단계 엑셀과 달라진 행의 수와 까닭.
- 4절에서 벗어난 것이 있으면 무엇을 왜.

## 12. 지시서와 달라진 점 (구현 뒤)
