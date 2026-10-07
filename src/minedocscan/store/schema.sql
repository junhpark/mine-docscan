-- minedocscan 스키마.
-- SQLite(개발·시험)와 PostgreSQL(운영) 양쪽에서 그대로 실행되는 부분집합만 쓴다.
--   · 날짜·시각은 ISO 8601 문자열(TEXT), 불리언은 INTEGER 0/1
--   · 쓰기는 store/db.py 의 upsert() (INSERT … ON CONFLICT … DO UPDATE) 만 사용
--
-- 세 층으로 나눈다.
--   doc_*   문서 층  — 파이프라인만 쓴다. 모든 값의 출처(페이지·좌표·신뢰도)를 여기서 추적한다
--   eq_*    마스터   — 장비. ISO 23725 FleetDefinition 구조를 따른다
--   insp_*, prod_*, xcheck_*  업무 층 — 2단계(통합DB·입력체계·대시보드)와 공유하는 면

-- ── 스키마 버전 ────────────────────────────────────────────────────────────
-- 마이그레이션은 만들지 않는다 (ADR 0005). 컬럼이 바뀌면 버전을 올리고, 버전이 다른 DB 는 열지 않고
-- "--fresh 로 다시 만드세요" 라고 알린다. 사람이 입력한 값은 검수 파일(reviews.jsonl)에 있으므로 잃지 않는다.
CREATE TABLE IF NOT EXISTS meta_schema (
  key             TEXT PRIMARY KEY,          -- schema_version
  value           TEXT NOT NULL
);

-- ── 문서 층 ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_document (
  document_id     TEXT PRIMARY KEY,          -- 원본 파일 SHA-256 앞 16자리: 같은 스캔의 중복 접수를 막는다
  source_path     TEXT NOT NULL,
  source_rel      TEXT,                      -- archive_root 기준 상대경로 (다른 컴퓨터에서도 원본을 찾기 위해). 밖이면 NULL
  source_name     TEXT NOT NULL,             -- 확장자를 뺀 파일명 (라벨·날짜 규칙의 키)
  work_date       TEXT,                      -- 문서의 날짜: 문서의 결정 > 문서 라벨(ISO 날짜만) > 파일명 규칙 (tasks/0007 4.2). NULL 이면 needs_date
  date_source     TEXT,                      -- 그 날짜의 출처: decision | label | filename | NULL(날짜 없음)
  n_pages         INTEGER,
  status          TEXT NOT NULL,             -- received(등록, 처리 전·처리 중) | needs_date(날짜를 기다린다) | processed | needs_review
                                             -- | failed(문서를 읽지 못함) | discarded(사람이 버렸다) — tasks/0007 4.1
  error           TEXT,                      -- failed 일 때 예외 종류와 메시지. 셀 값은 적지 않는다
  warning         TEXT,                      -- 처리는 했지만 알아야 할 것 (예: damaged_pdf = warn 으로 복구해서 연 PDF)
  created_at      TEXT NOT NULL,
  received_at     TEXT,                      -- 등록한 시각 (UTC). 다시 처리해도 바뀌지 않는다 — upsert 가 덮어쓰지 않는다 (insert_only)
  work_requested  INTEGER NOT NULL DEFAULT 0,   -- 다시 처리해 달라는 요청 번호 (요청마다 +1). upsert 로 쓰지 않는다 — 전용 UPDATE 로만
  work_done       INTEGER NOT NULL DEFAULT 0    -- 처리를 시작할 때 읽은 요청 번호 (끝날 때 적는다). 대기 = received 이거나 requested > done
);

CREATE TABLE IF NOT EXISTS doc_page (
  page_id         TEXT PRIMARY KEY,          -- document_id || '-p' || page_no
  document_id     TEXT NOT NULL REFERENCES doc_document(document_id),
  page_no         INTEGER NOT NULL,
  template_name   TEXT,                      -- 분류된 양식. NULL = 어느 양식에도 맞지 않음
  classify_margin REAL,                      -- 1위/2위 인라이어 비율
  align_inliers   INTEGER,
  align_grid_err  REAL,                      -- 괘선 재검출 오차(px)
  align_ok        INTEGER,                   -- 0/1, 정합을 시도하지 않았으면 NULL
  aligned_image   TEXT,                      -- 정합 이미지 경로 (work_root 기준 상대경로)
  homography      TEXT,                      -- JSON 3×3: 렌더링한 쪽 픽셀 → 템플릿 픽셀. 원본 해상도 크롭이 쓴다 (imaging/hires.py)
  render_dpi      INTEGER,                   -- 그 호모그래피를 구할 때 쪽을 렌더링한 해상도 (이미지 파일이면 원본 그대로)
  print_sha       TEXT,                      -- 값 유무를 잰 인쇄 층의 해시 (Template.print_sha — 화소의 해시). 쓰지 않았으면 NULL (tasks/0006 4.3)
  variant_errs    TEXT,                      -- 동시 판마다의 괘선 오차 JSON {판 이름: 오차 | null}. 판이 하나면 NULL (tasks/0006 4.6)
  rotation        INTEGER,                   -- 0 | 90 | 180 | 270: 시계 방향으로 그만큼 돌리면 바로 선다. 정합하지 않은 쪽은 NULL (tasks/0007 4.4)
  duplicate_of    TEXT,                      -- duplicate 일 때 먼저 들어온 그 쪽 (외래 키 없음 — 가리키는 쪽이 먼저 지워질 수 있다, 4.6)
  duplicate_sim   REAL,                      -- 그 쪽과의 손글씨 자리 유사도
  work_date       TEXT,
  status          TEXT NOT NULL,             -- unknown_form | blank | classified_only | align_failed | duplicate | loaded | discarded | error
  error           TEXT                       -- error 일 때 예외 종류와 메시지. 그 쪽의 반쯤 쓰인 행은 남기지 않는다(SAVEPOINT)
);
CREATE INDEX IF NOT EXISTS ix_doc_page_document ON doc_page(document_id);

CREATE TABLE IF NOT EXISTS doc_field (
  field_id        TEXT PRIMARY KEY,          -- page_id:region:field_name:row
  page_id         TEXT NOT NULL REFERENCES doc_page(page_id),
  region          TEXT NOT NULL,
  row_no          INTEGER NOT NULL,          -- 자유 필드는 -1
  field_name      TEXT NOT NULL,
  kind            TEXT NOT NULL,             -- printed | handwritten_text | handwritten_number | checkmark | signature
  format          TEXT,                      -- 값의 형식: integer | decimal | time | time_range | reading | NULL(글자) — 템플릿에서
  row_key         TEXT,
  x0 INTEGER, y0 INTEGER, x1 INTEGER, y1 INTEGER,   -- 템플릿 좌표계 bbox → 출처 추적
  ink             REAL,                      -- 잉크 비율
  has_value_raw   INTEGER,                   -- 기계가 판단한 값 유무 (검수해도 바뀌지 않는다)
  has_value       INTEGER,                   -- 최종 값 유무 (검수가 있으면 검수가 정한다)
  value_raw       TEXT,                      -- 인식 원문 — 언제나 기계의 것
  value_final     TEXT,                      -- 교정·검수 후 값
  confidence      REAL,                      -- 기계의 신뢰도
  candidates      TEXT,                      -- JSON 배열
  backend         TEXT,                      -- 값을 만든 주체: template | ink | <인식 백엔드 이름>
  review_status   TEXT NOT NULL,             -- auto | pending | reviewed
  status_raw      TEXT,                      -- 기계가 정한 상태 (auto | pending) — 검수해도 바뀌지 않는다. 자동 적재 오류율의 분모
  reviewed_by     TEXT,
  reviewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_doc_field_page ON doc_field(page_id);
CREATE INDEX IF NOT EXISTS ix_doc_field_review ON doc_field(review_status);

-- 쪽의 메타(날짜·차량번호·작성자 …) — 쪽 × 키마다 한 행. 값이 어디서 왔는지와 기계가 읽은 값을 같이 남긴다 (tasks/0004 4.3, 4.4).
-- 우선순위: 검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙 > 기계가 읽은 값(자동 적재 기준을 넘은 것만).
-- 위에 값이 있으면 기계 값은 대조에만 쓴다 (check_result). 기계 열은 검수가 건드리지 않는다.
-- prod_haul(일보)의 차량·작성자, 자리, eq_assignment_obs 는 이 테이블의 최종 값(value)에서 만든다.
CREATE TABLE IF NOT EXISTS doc_page_meta (
  page_id            TEXT NOT NULL REFERENCES doc_page(page_id),
  meta_key           TEXT NOT NULL,          -- date | vehicle_no | operator | date.month | date.day | …
  value              TEXT,                   -- 최종 값. NULL = 모름
  source             TEXT,                   -- review | label | filename | machine | NULL(값 없음)
  field_id           TEXT,                   -- 이 키를 적는 자유 필드 (템플릿의 meta_key). 없으면 NULL
  machine_value      TEXT,                   -- 기계가 읽은 값 (목록에 없는 값이면 자유롭게 읽은 문자열)
  machine_confidence REAL,
  machine_status     TEXT,                   -- auto | pending | unlisted | empty(잉크 없음) | NULL(읽는 모델 없음)
  check_result       TEXT NOT NULL,          -- match | mismatch | unread(위에 값이 있는데 기계가 정하지 못함) | none(대조할 것 없음)
  PRIMARY KEY (page_id, meta_key)
);
CREATE INDEX IF NOT EXISTS ix_doc_page_meta_check ON doc_page_meta(check_result);

-- 사람이 입력한 값. 원본은 사이트 팩의 추가 전용 파일(reviews/reviews.jsonl)이고 이 테이블은 그 사본이다
-- (WORK_ROOT 는 언제든 지울 수 있어야 하므로). 한 필드에 여러 건이면 reviewed_at 이 가장 늦은 것이 유효하다.
-- field_id 에 외래 키를 걸지 않는다: 파일을 읽어 들이는 시점에 그 페이지가 아직 DB 에 없을 수 있다.
CREATE TABLE IF NOT EXISTS doc_review (
  review_id       TEXT PRIMARY KEY,          -- field_id, reviewed_at, reviewer (note 가 있으면 note 도) 에서 결정된다
  field_id        TEXT NOT NULL,
  page_id         TEXT NOT NULL,             -- field_id 의 앞부분 (조회용)
  seq             INTEGER NOT NULL,          -- 파일에서의 줄 번호. 같은 시각이면 뒤의 것이 유효
  verdict         TEXT NOT NULL,             -- value | empty | illegible
  value           TEXT,                      -- verdict = value 일 때 종이에 적힌 그대로
  reviewer        TEXT NOT NULL,
  reviewed_at     TEXT NOT NULL,             -- ISO 8601 UTC
  note            TEXT,
  -- 여기부터는 DB 없이도 사람이 읽고 학습 데이터를 만들 수 있게 하는 문맥 (검수 당시의 값)
  source          TEXT,                      -- "<파일명>#<쪽>"
  template        TEXT,
  region          TEXT,
  field_name      TEXT,
  row_no          INTEGER,
  row_key         TEXT,
  x0 INTEGER, y0 INTEGER, x1 INTEGER, y1 INTEGER,   -- 검수 당시의 bbox. 템플릿을 고치면 달라질 수 있다
  machine_has_value  INTEGER,                -- 검수 당시 기계가 낸 값
  machine_value_raw  TEXT,
  machine_backend    TEXT,
  machine_confidence REAL
);
CREATE INDEX IF NOT EXISTS ix_doc_review_field ON doc_review(field_id);
CREATE INDEX IF NOT EXISTS ix_doc_review_page ON doc_review(page_id);

-- 문서·쪽에 대해 사람이 정한 것 (tasks/0007 4.3). 원본은 검수 파일 옆의 추가 전용 파일(decisions.jsonl)이고 이 테이블은 그 사본이다.
-- 유효한 결정 = 대상·묶음(date / discard·restore / keep)마다 파일에서 가장 뒤의 것. 결정은 업무 테이블을 직접 고치지 않는다 —
-- 그 문서의 다시 처리(work_requested)로 적용한다. 대상에 외래 키를 걸지 않는다: 파일을 읽어 들일 때 그 문서가 아직 없을 수 있다.
CREATE TABLE IF NOT EXISTS doc_decision (
  decision_id     TEXT PRIMARY KEY,
  seq             INTEGER NOT NULL,          -- 파일에서의 줄 번호 (뒤의 것이 이긴다)
  target          TEXT NOT NULL,             -- document_id 또는 page_id(document_id-pN)
  document_id     TEXT NOT NULL,             -- 대상의 문서 (쪽이면 그 문서)
  page_no         INTEGER,                   -- 쪽이면 쪽 번호, 문서면 NULL
  kind            TEXT NOT NULL,             -- date | discard | restore | keep
  value           TEXT,                      -- date 의 ISO 날짜
  decided_by      TEXT NOT NULL,
  decided_at      TEXT NOT NULL,             -- ISO 8601 UTC
  note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_doc_decision_doc ON doc_decision(document_id);

-- 다시 스캔한 쪽을 알아보는 서명 (tasks/0007 4.6): 적재된 쪽만. 정합 그림에서 인쇄를 지운 손글씨를 16 px 칸마다 센 것.
-- 쪽마다 2만 자 안팎이라 doc_page 에 넣지 않는다.
CREATE TABLE IF NOT EXISTS doc_page_sig (
  page_id         TEXT PRIMARY KEY,
  document_id     TEXT NOT NULL,
  family          TEXT NOT NULL,             -- 템플릿의 family, 없으면 템플릿 이름 — 같은 계열 안에서만 비교한다
  work_date       TEXT NOT NULL,             -- 같은 날 안에서만 비교한다
  sig             TEXT NOT NULL              -- 칸의 수 "가로x세로:" + 칸마다의 잉크 화소 수 (쉼표로)
);
CREATE INDEX IF NOT EXISTS ix_doc_page_sig_day ON doc_page_sig(work_date, family);

-- ── 마스터 ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS eq_equipment (
  equipment_id    TEXT PRIMARY KEY,          -- UUID (ISO 23725 EquipmentId). 같은 키는 항상 같은 UUID
  equipment_key   TEXT NOT NULL UNIQUE,      -- 사이트 팩의 행 키
  hid             TEXT NOT NULL,             -- 사람이 읽는 이름 (ISO 23725 HID)
  site_category   TEXT,                      -- 현장의 장비 구분
  iso_type        TEXT,                      -- ISO 23725 Table 11 (Drill, Loader, Excavator …). 대응이 없으면 NULL
  oem             TEXT,
  model           TEXT,
  registration    TEXT,
  active          INTEGER NOT NULL DEFAULT 1
);

-- 그날 실제로 누가 어느 차를 몰았는가. 인쇄된 양식 머리글은 만든 당시 상태로 굳어 있으므로 믿지 않는다.
CREATE TABLE IF NOT EXISTS eq_assignment_obs (
  work_date         TEXT NOT NULL,
  slot              TEXT NOT NULL,           -- 행렬 양식의 열(자리) 식별자
  vehicle_no        TEXT,                    -- 차량별 일보에 손으로 적힌 값
  operator          TEXT,
  header_vehicle_no TEXT,                    -- 행렬 양식에 인쇄된 값
  header_operator   TEXT,
  matched_by        TEXT NOT NULL,           -- operator | vehicle
  header_mismatch   INTEGER NOT NULL,        -- 인쇄된 머리글과 실제가 다른가
  PRIMARY KEY (work_date, slot)
);

-- ── 업무 층 ────────────────────────────────────────────────────────────────
-- 일일 장비 점검: 직접 입력(2단계)과 스캔 입력이 같은 테이블에 들어간다
CREATE TABLE IF NOT EXISTS insp_daily (
  inspection_id   TEXT PRIMARY KEY,          -- inspection_date:equipment_id
  inspection_date TEXT NOT NULL,
  equipment_id    TEXT NOT NULL REFERENCES eq_equipment(equipment_id),
  abnormal        INTEGER,                   -- 1=유, 0=무, NULL=판정 불가
  remark          TEXT,
  entry_source    TEXT NOT NULL,             -- scan | manual
  page_id         TEXT,                      -- 이 행을 만든 쪽 (scan). 같은 날 두 쪽이 같은 장비를 적으면 쪽의 순서가 뒤인 쪽 (tasks/0007 4.8)
  source_field_id TEXT REFERENCES doc_field(field_id),
  review_status   TEXT NOT NULL              -- auto | pending | reviewed
);
CREATE INDEX IF NOT EXISTS ix_insp_daily_date ON insp_daily(inspection_date);
CREATE INDEX IF NOT EXISTS ix_insp_daily_page ON insp_daily(page_id);       -- 문서를 지우고 다시 만들 때 (tasks/0007 4.8)

-- 운반 실적: 같은 (날짜, 차량, 광종, 편) 값이 차량별 일보와 편×차량 행렬 양쪽에서 들어온다
CREATE TABLE IF NOT EXISTS prod_haul (
  haul_id         TEXT PRIMARY KEY,          -- source_field_id 와 같다
  work_date       TEXT,
  source_form     TEXT NOT NULL,             -- 템플릿 이름
  source_role     TEXT NOT NULL,             -- log = 차량별 일보 | matrix = 편×차량 행렬
  page_id         TEXT NOT NULL REFERENCES doc_page(page_id),
  slot            TEXT,                      -- matrix: 열 식별자. log: 교차검증 단계에서 채운다
  vehicle_no      TEXT,                      -- matrix: 인쇄된 머리글 값. log: 손으로 적힌 값
  operator        TEXT,
  material        TEXT NOT NULL,
  level           TEXT NOT NULL,
  shift           TEXT,                      -- day | night | NULL
  has_value_raw   INTEGER,                   -- 기계가 판단한 값 유무 (교차검증의 기계 값 합에서 "모름" 판단에 쓴다)
  has_value       INTEGER NOT NULL,          -- 최종 값 유무
  trips           INTEGER,                   -- 최종 횟수 (검수가 있으면 검수값, 없으면 기계가 읽은 값)
  trips_raw       INTEGER,                   -- 기계가 읽은 횟수 (인식기가 없으면 NULL). 검수해도 바뀌지 않는다
  confidence      REAL,
  source_field_id TEXT REFERENCES doc_field(field_id),
  review_status   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prod_haul_date ON prod_haul(work_date);
CREATE INDEX IF NOT EXISTS ix_prod_haul_page ON prod_haul(page_id);

-- 양식 간 교차검증: 같은 값이 두 문서에 적힌 경우의 일치 여부. 불일치는 검수 큐로 간다
CREATE TABLE IF NOT EXISTS xcheck_haul (
  work_date    TEXT NOT NULL,
  slot         TEXT NOT NULL,
  material     TEXT NOT NULL,
  level        TEXT NOT NULL,
  operator     TEXT,
  vehicle_no   TEXT,
  log_has      INTEGER,                      -- 차량별 일보 셀의 값 유무 (NULL = 그 차량의 일보가 없음)
  matrix_has   INTEGER,                      -- 행렬 셀의 값 유무
  log_trips    INTEGER,                      -- 최종 값 기준 (status 는 이것으로 판정)
  matrix_trips INTEGER,
  log_trips_raw    INTEGER,                  -- 기계 값 기준 — 인식기끼리 비교할 때 본다
  matrix_trips_raw INTEGER,
  status       TEXT NOT NULL,                -- match | mismatch | missing_log | missing_matrix
  PRIMARY KEY (work_date, slot, material, level)
);

-- 장비 가동 일보 (tasks/0005 4.3): 쪽 하나에 한 행. 한 장비가 하루에 두 장을 내면 두 행이다 — 합치지 않는다 (읽는 쪽의 일).
-- 값은 검수를 적용한 최종 필드 행에서 만든다. *_raw 는 기계가 읽은 값(지금은 소수·시각을 읽는 모델이 없어 NULL — 미룸, tasks/0006 1절).
-- hours 는 적힌 값에서 계산한 것이고 무엇으로 계산했는지가 hours_basis 다: meter(종료 − 시작) > total(총 칸) > clock(시각)
-- > shifts(근무 시각 범위의 합) > NULL. 추정하지 않는다 — 앞의 근거에 아직 모르는 칸(검수 대기)이 있거나 계기 칸에 숫자와
-- 시각이 섞였으면 NULL. equipment_id 는 사이트 팩의 대응표([equipment.aliases])로만 정한다 (없으면 NULL, 이름은 남는다).
-- equipment_id 에 외래 키를 걸지 않는다: 마스터 행(eq_equipment)은 점검표 쪽을 적재할 때 생기므로 가동 일보만 돌린 DB 에는 없다.
CREATE TABLE IF NOT EXISTS eq_usage_daily (
  page_id         TEXT PRIMARY KEY REFERENCES doc_page(page_id),
  work_date       TEXT,
  source_form     TEXT NOT NULL,             -- 템플릿 이름
  equipment       TEXT,                      -- 적힌 장비명 (쪽 메타 equipment 의 최종 값)
  equipment_id    TEXT,                      -- 대응표로 정한 장비 ID (eq_equipment.equipment_id 와 같은 UUID). 모르면 NULL
  operator        TEXT,                      -- 쪽 메타 operator 의 최종 값
  reading_kind    TEXT NOT NULL,             -- 계기 칸: meter(계기 값) | clock(시각) | mixed(섞임) | empty(빈 칸) | pending(모르는 칸) | none(계기 표 없음)
  meter_start     REAL,                      -- 계기 값 (reading_kind 가 meter 일 때)
  meter_end       REAL,
  meter_total     REAL,
  clock_start     TEXT,                      -- 계기 칸에 적은 시각 HH:MM (reading_kind 가 clock 일 때)
  clock_end       TEXT,
  meter_start_raw TEXT,                      -- 기계가 읽은 계기 칸 (value_raw). 검수해도 바뀌지 않는다
  meter_end_raw   TEXT,
  meter_total_raw TEXT,
  shifts          TEXT,                      -- 근무 시각 범위 JSON {"<행 키>": "08:00~12:00", …} (값이 있는 칸만). 근무 표가 없으면 NULL
  shift_minutes   INTEGER,                   -- 범위 길이의 합(분). 모르는 칸이 있거나 값이 없으면 NULL
  activity_rows   INTEGER,                   -- 작업 표에서 글씨가 있는 줄의 수 (소계 줄 제외). 작업 표가 없으면 NULL
  signed          INTEGER,                   -- 서명 칸에 잉크가 있는가 (서명 칸이 없으면 NULL)
  hours           REAL,                      -- 가동 시간 (시간)
  hours_basis     TEXT,                      -- meter | total | clock | shifts | NULL
  start_field_id  TEXT REFERENCES doc_field(field_id),   -- 출처: 계기 칸 셋
  end_field_id    TEXT REFERENCES doc_field(field_id),
  total_field_id  TEXT REFERENCES doc_field(field_id),
  review_status   TEXT NOT NULL              -- 계기·근무 칸 중 하나라도 pending 이면 pending, 아니고 reviewed 가 있으면 reviewed, 아니면 auto
);
CREATE INDEX IF NOT EXISTS ix_eq_usage_daily_date ON eq_usage_daily(work_date);

-- 작업량 표 (role tally): 숫자 칸 하나에 한 행. 소계 칸(is_subtotal)도 적힌 대로 남긴다 — 합칠 때 빼는 것은 읽는 쪽.
CREATE TABLE IF NOT EXISTS prod_tally (
  tally_id        TEXT PRIMARY KEY,          -- source_field_id 와 같다
  work_date       TEXT,
  page_id         TEXT NOT NULL REFERENCES doc_page(page_id),
  source_form     TEXT NOT NULL,
  equipment       TEXT,                      -- 쪽 메타 (eq_usage_daily 와 같다)
  equipment_id    TEXT,
  item            TEXT NOT NULL,             -- 행 메타 item (없으면 행 키)
  place           TEXT,                      -- 행 메타 place (하단 / 저광장 …)
  column_name     TEXT NOT NULL,             -- 열 이름
  shift           TEXT,                      -- 열 메타 shift
  is_subtotal     INTEGER NOT NULL,          -- 소계 칸 (열이나 행의 subtotal: true)
  has_value_raw   INTEGER,
  has_value       INTEGER NOT NULL,
  count           INTEGER,                   -- 최종 값
  count_raw       INTEGER,                   -- 기계가 읽은 값. 검수해도 바뀌지 않는다
  confidence      REAL,
  source_field_id TEXT REFERENCES doc_field(field_id),
  review_status   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prod_tally_page ON prod_tally(page_id);

-- 가동 일보의 검산 (tasks/0005 4.4): 한 행 = 검산 하나. 보여 주기만 한다 — eq_usage_daily 의 값을 고치지 않는다 (ADR 0006).
--   total       쪽 안: 총 = 종료 − 시작. value_a = 총 칸, value_b = 종료 − 시작                         match | mismatch
--   subtotal    쪽 안: 소계 = 합 (item = 소계 칸의 field_id). value_a = 소계, value_b = 합              match | mismatch | unknown
--   continuity  날짜 사이: value_a = 이 기록의 시작, value_b = 같은 장비의 바로 앞 기록의 종료 (other_page_id 의 쪽)
--               match | gap(시작이 더 크다) | overlap(더 작다) | first(앞 기록 없음) | unknown(장비를 모른다, 검수 대기)
-- diff = value_a − value_b. 허용 오차 0.05 시간 (소수 한 자리의 반올림). days_between: 두 기록 사이에 낀 날 수.
CREATE TABLE IF NOT EXISTS xcheck_usage (
  page_id        TEXT NOT NULL,
  check_kind     TEXT NOT NULL,              -- total | subtotal | continuity
  item           TEXT NOT NULL,              -- subtotal: 소계 칸의 field_id. 그 밖은 '' (PK 에 NULL 을 두지 않는다)
  work_date      TEXT,
  equipment_ref  TEXT,                       -- continuity: 같은 장비의 기준 — "id:<장비 ID>" 또는 "name:<적힌 이름>". 모르면 NULL
  value_a        REAL,
  value_b        REAL,
  diff           REAL,
  days_between   INTEGER,
  result         TEXT NOT NULL,
  other_page_id  TEXT,                       -- continuity: 비교 상대(앞 기록)의 쪽
  field_a        TEXT,                       -- 비교한 칸 (출처): 이 쪽의 칸
  field_b        TEXT,                       -- 상대 칸 (앞 기록의 종료 칸, 이 쪽의 종료 칸)
  PRIMARY KEY (page_id, check_kind, item)
);
CREATE INDEX IF NOT EXISTS ix_xcheck_usage_ref ON xcheck_usage(equipment_ref);
CREATE INDEX IF NOT EXISTS ix_xcheck_usage_result ON xcheck_usage(result);
CREATE INDEX IF NOT EXISTS ix_xcheck_usage_page ON xcheck_usage(page_id);
