# 사이트 팩

사이트 팩은 한 현장의 양식 정의·옵션·라벨 묶음이다. 소프트웨어는 현장을 모르고, 현장에 관한 것은 전부 여기서 읽는다.
**사이트 팩은 저장소에 넣지 않는다** — 기준 이미지와 템플릿 머리글에 이름·서명·차량번호가 들어 있다 ([DATA.md](DATA.md)).

합성 사이트 팩을 만들어 보면 형식을 바로 볼 수 있다: `minedocscan synth out/demo` → `out/demo/site/`.

```
<site>/
  site.toml                        현장 이름, 파일명 규칙, 장비 구분 대응, 교차검증 옵션
  templates/<양식>/template.yaml   양식 정의
  templates/<양식>/reference.png   기준 이미지 (빈 양식 또는 깨끗한 스캔 한 장, 200 dpi)
  labels/pages.json                사람이 붙인 페이지 메타 (선택)
  reviews/reviews.jsonl            검수 기록 — 사람이 입력한 값 (추가 전용, 도구가 쓴다)
  models/<이름>/                   인식기 모델 (선택, `minedocscan recognizer train` 이 쓴다): model.onnx, card.json, train-log.jsonl
                                   (메타 필드 모델은 classes.json 도 — 이름·차량번호가 들어 있다)
  expected/regression.json         회귀 기준 수치 (선택, `minedocscan regress --update` 가 쓴다)
```

## site.toml

```toml
[site]
name = "synthetic"                 # 짧은 식별자
title = "…"                        # 사람이 읽는 이름

[ingest]
# 파일명에서 날짜를 뽑는 정규식. 이름 있는 그룹 yyyy 또는 yy, mm, dd
date_from_filename = '(?P<yyyy>\d{4})-(?P<mm>\d{2})-(?P<dd>\d{2})'

[equipment.iso_type]
# 현장의 장비 구분 → ISO 23725 장비 유형. 대응을 확인하지 못한 구분은 적지 않는다 (NULL 로 남는다)
"Loader" = "Loader"

[equipment.aliases]
# 가동 일보에 손으로 적는 장비명 → 장비 키 (점검표 템플릿의 장비 행 key). 대응을 모르는 이름은 적지 않는다 —
# 그 쪽의 장비 ID 는 NULL 이고 이름은 남는다. 비슷한 이름으로 맞추지 않는다 (적힌 이름 그대로, 앞뒤 빈칸만 뗀다).
# 마스터(점검표의 장비 행)에 없는 키를 가리키면 사이트 팩을 읽을 때 오류다 (몇 번째 항목인지만 알린다)
"LOADER" = "EQ-0301"

[haul]
# 운반 횟수의 범위. 이보다 큰 값은 숫자 인식기의 신뢰도가 높아도 검수로 보낸다 (ADR 0012). 없으면 검사하지 않는다
trips_max = 40

[crosscheck.haul]
# 교차검증에서 뺄 광종 (한쪽 양식에만 있는 행)
exclude_materials = ["SURFACE"]

[recognize.digits]
# 숫자 인식기 모델 (models/<이름>). 설정 파일의 같은 항목이 이긴다 — 현장 PC 마다 다르게 둘 필요가 없으면 여기에.
# 자동 적재 기준을 카드와 다르게 하려면 설정 파일의 [recognize.digits] auto_accept_conf (사이트 팩에는 두지 않는다)
# model = "digits-v1"

[eval]
# 평가셋 분할 (ADR 0009): 날짜 단위, hash(split_salt, 날짜) < test_share 면 test. 없으면 소금값은 사이트 이름, 비율 0.2.
# 소금값을 바꾸는 것은 평가셋을 버리는 것이다 — 그 전의 수치와 비교하지 않는다
split_salt = "synthetic-2030"
test_share = 0.2
```

코드에서는 `site.option("crosscheck.haul", "exclude_materials", [])` 처럼 읽는다. 새 옵션이 필요하면 여기에 절을 추가한다.

## template.yaml

```yaml
name: synth_haul_log               # 템플릿 이름 (영문 소문자·밑줄). DB 의 template_name
title: Dump truck daily haul log   # 사람이 읽는 이름
# family: haul_matrix              # 선택: 같은 양식의 개정판 묶음. valid_from / valid_to (YYYY-MM-DD, 양 끝 포함) 로 가린다
# valid_to: 2030-01-07
reference_image: reference.png
dpi: 200                           # 좌표계의 해상도. 설정의 dpi 와 같아야 한다
page_size: [2339, 1654]            # 기준 이미지의 (폭, 높이) px
handler: haul                      # generic | inspection | haul | usage
handler_options:
  role: log
  region: haul

regions:                           # 한 페이지에 표가 여러 개일 수 있다
  - name: haul
    grid:
      ys: [380, 450, 530, …]       # 수평 괘선의 y (오름차순). 행 r 은 ys[r + header_rows] ~ ys[r + header_rows + 1]
      xs: [150, 400, 600, 850, 1100]   # 수직 괘선의 x (오름차순). 열 idx 는 xs[idx] ~ xs[idx + 1]
    header_rows: 1                 # 머리글 줄 수 (데이터 행 번호는 그 아래부터 0)
    columns:
      - {idx: 0, name: material,    kind: printed}
      - {idx: 1, name: level,       kind: printed}
      - {idx: 2, name: trips_day,   kind: handwritten_number, shift: day}
      - {idx: 3, name: trips_night, kind: handwritten_number, shift: night}
    rows:
      - {row: 0, key: "ORE|L0", material: ORE, level: L0}
      - {row: 1, key: "ORE|L1", material: ORE, level: L1}

fields:                            # 표 밖의 자유 필드
  - {name: vehicle_no, kind: handwritten_text, bbox: [340, 290, 730, 355], meta_key: vehicle_no}   # x0, y0, x1, y1
  - {name: operator,   kind: handwritten_text, bbox: [920, 290, 1290, 355], meta_key: operator}
```

`meta_key` 가 있는 자유 필드의 값은 그 쪽의 메타가 된다 (검수 화면 `--queue page-fields`, 또는 메타 필드 모델 — 아래 `[recognize.meta]`).
쪽 메타의 우선순위는 **검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙 > 기계 값** 이다. 기계 값은 위에 값이 없을 때만 쓰이고, 있으면 대조만 한다
(DB 의 `doc_page_meta`, ARCHITECTURE §5). `date` 는 받지 않는다 — 날짜는 파일명 규칙과 문서 라벨로 정한다.
실제 사이트 팩의 일보 템플릿에는 위 두 줄처럼 차량번호·작성자 필드에 `meta_key` 를 넣는다 (bbox 는 그 양식의 값으로).
한 템플릿에서 같은 `meta_key` 를 두 필드에 주면 사이트 팩을 읽을 때 오류다.

**손으로 쓴 월·일** (선택). 양식의 날짜 줄에 월·일을 손으로 적는 칸이 있으면 `date.month`·`date.day` 필드를 넣는다. 날짜를 정하지는 않고
기계가 읽은 값을 쪽의 날짜(파일명·라벨)와 **대조만** 한다 — 다른 날의 쪽이 묶음에 섞인 것을 찾는다. 검수로 받지 않으므로 `page-fields` 에도 나오지 않는다.
`date.` 뒤에는 `month`·`day` 만 된다.

```yaml
fields:
  - {name: date_month, kind: handwritten_number, bbox: [530, 205, 670, 270], meta_key: date.month}
  - {name: date_day,   kind: handwritten_number, bbox: [775, 205, 915, 270], meta_key: date.day}
```

### 값의 형식 (`format`)

손으로 쓰는 칸·필드(`handwritten_number`, `handwritten_text`)에 선택 항목 `format` (ADR 0015). 인쇄된 칸·체크·서명에 주면 템플릿 오류다.

| format | 적는 것 | DB 에 남는 표기 | 검수 화면이 받는 입력의 예 |
|---|---|---|---|
| `integer` | 정수 (`handwritten_number` 의 기본) | `7` | `7`, `07` |
| `decimal` | 소수 | `1234.5` | `1234.5`, `01234.5` |
| `time` | 시각 | `08:00` | `8:00`, `08:00`, `0800`, `08.00` |
| `time_range` | 시각 범위 (끝이 시작보다 이르면 다음 날) | `08:00~17:00` | `8-17`, `08:00~17:00`, `0800-1700` |
| `reading` | 계기 값 또는 시각 | 콜론이 없으면 `decimal`, 있으면 `time` | `1234.5`, `08:00` |
| (없음) | 글자 (`handwritten_text` 의 기본) | 적힌 대로 | |

`integer` 가 아닌 칸은 지금의 숫자 인식기에 보내지 않는다 — 잉크가 있으면 검수 대기다 (계기 칸은 `review serve --queue readings`).
계기 칸의 규칙: 계기 값은 숫자 그대로(1234.5), 시각은 콜론으로(08:00) — 점으로 쓴 시각(08.00)도 콜론으로 입력. 숫자인지 시각인지는 사람이 정한다.

### 열의 종류 (`kind`)

| kind | 처리 |
|---|---|
| `printed` | 읽지 않는다. 행 메타에서 같은 이름의 값을 가져온다 (`rows[].<열 이름>`) |
| `handwritten_text` | 잉크가 있으면 인식 백엔드로 보낸다 |
| `handwritten_number` | 위와 같되 숫자로 해석한다. `haul` 핸들러와 `usage` 의 `meter`·`shifts`·`tally` 표에서는 글씨 덩어리 배정으로 값 유무를 정한다. 형식이 `integer` 가 아니면 인식기에 보내지 않는다 |
| `checkmark` | ✓ 유무. `inspection` 핸들러에서는 나란한 두 칸을 한 쌍으로 판정한다 |
| `signature` | 유무만 기록한다 |

### 행

- `row` 는 머리글을 뺀 데이터 행 번호(0부터), `key` 는 그 행을 식별하는 값(장비 등록번호, `광종|편` 등)이다.
- 한 표 안에서 `key` 는 겹치면 안 된다. 양식의 여백 행은 `key` 를 비워 두면 `#<행 번호>` 로 구분된다.
- `key` 외의 항목은 행 메타다. `printed` 열의 값과 핸들러가 쓰는 값을 여기에 적는다.

### 핸들러별 약속

**`inspection`** — 행 = 장비.
- `handler_options`: `region`, `yes_column`(기본 `abnormal_yes`, **왼쪽 칸**), `no_column`(기본 `abnormal_no`), `text_column`(기본 `remark`)
- 행 메타: `key`(장비 키), `category`(현장의 장비 구분), `model`, `registration`. 형식과 등록번호가 모두 빈 행은 여백 행으로 본다.
- 날짜가 있어야 `insp_daily` 에 들어간다 (파일명 규칙 또는 라벨).

**`haul`** — 운반 횟수.
- `handler_options`: `role`(`log` 또는 `matrix`), `region`(횟수 셀이 있는 표)
- 행 메타: `material`, `level`
- 열 메타: `role: log` 는 `shift`(`day`/`night`), `role: matrix` 는 `slot`, `header_vehicle_no`, `header_operator`
- `role: log` 는 페이지마다 차량번호·작성자가 필요하다. `meta_key` 필드를 검수 화면에서 입력하거나(권장), 라벨로 주거나, 메타 필드 모델이 읽는다.
  행렬의 `header_vehicle_no`·`header_operator` 는 그 화면의 후보 목록이 된다.

**`usage`** — 장비 가동 일보: 장비 한 대의 하루 (ADR 0016). `handler_options` 는 없다. 표마다 **역할**(`role`)을 적는다:

| `role` | 칸 | 필요한 것 |
|---|---|---|
| `meter` | 계기의 시작·종료·총 | 열 이름 `start`·`end`(필수)·`total`, 형식 `reading`(또는 `decimal`·`time`). 세로로 놓인 계기 칸이면 행 키 `start`·`end`·`total` 과 손으로 쓰는 열 하나. 한 템플릿에 하나 |
| `shifts` | 근무 구분(행 키 — `am`, `pm`, `ot` …) × 시각 범위 | 형식 `time_range` 인 열. 한 템플릿에 하나 |
| `tally` | 작업량: 구분 × 근무조·장소의 정수 | 정수 칸. 행 메타 `item`(구분)·`place`(장소), 열 메타 `shift`. 소계 칸은 열(또는 행) 메타 `subtotal: true` — 검산 소계 = 합 |
| `activities` | 작업 표 (작업위치·작업내용·운행시간·비고 …) | 글자 칸. 글씨가 있는 줄의 수만 업무 테이블에 간다. 합계 줄은 행 메타 `subtotal: true` (세지 않는다) |

- 표 밖 필드: 장비명 `meta_key: equipment`, 운전자 `meta_key: operator`, 서명 `kind: signature`, 연료·오일·특이사항은 글자 필드(`doc_field` 에만).
  장비명은 `[equipment.aliases]` 로 장비 ID 와 잇는다 (위 site.toml).
- **칸 안의 인쇄된 줄로 행 나누기**: 로우더 작업일보처럼 한 칸에 "하단: _ 대 / 저광장: _ 대" 가 인쇄되어 있으면, 인쇄된 줄마다 템플릿 행을 나누고
  그 사이의 선을 `grid.split_ys` 에 적는다 (열이면 `split_xs`). 나눔 선은 칸을 자르는 데만 쓰고 정합 판정·괘선 지우기에는 쓰지 않는다 —
  `ys` 에 없는 괘선을 적으면 정합이 그 선을 찾다가 쪽 전체를 `align_failed` 로 만든다. `row`·`idx` 는 괘선과 나눔 선을 합친 순서다.
  인쇄된 글자("하단:", "대")가 값 칸 안에 들어가면 늘 잉크로 보인다 — 그 글자는 `split_xs` 로 따로 떼어 `printed` 칸으로 둔다.

합성 예 (`minedocscan synth out/u --usage-only` 의 `synth_loader_log`, 줄임):

```yaml
handler: usage
regions:
  - name: tally
    role: tally
    grid: {ys: [360, 420, 508, 596, …], xs: [150, 400, 560, 720, 880, 1040], split_ys: [464, 552, …]}
    header_rows: 1
    columns:
      - {idx: 0, name: item,  kind: printed}
      - {idx: 1, name: place, kind: printed}
      - {idx: 2, name: a,     kind: handwritten_number, shift: A}
      - {idx: 3, name: ot,    kind: handwritten_number, shift: OT}
      - {idx: 4, name: sub,   kind: handwritten_number, subtotal: true}
    rows:
      - {row: 0, key: "ORE|LOW",  item: ORE, place: LOW}
      - {row: 1, key: "ORE|YARD", item: ORE, place: YARD}
  - name: shifts
    role: shifts
    grid: {ys: [360, 420, 480, 540, 600], xs: [1200, 1400, 1840]}
    header_rows: 1
    columns:
      - {idx: 0, name: shift, kind: printed}
      - {idx: 1, name: range, kind: handwritten_number, format: time_range}
    rows: [{row: 0, key: am, shift: AM}, {row: 1, key: pm, shift: PM}, {row: 2, key: ot, shift: OT}]
  - name: meter
    role: meter
    grid: {ys: [700, 742, 788], xs: [1200, 1570, 1940, 2310]}
    header_rows: 1
    columns:
      - {idx: 0, name: start, kind: handwritten_number, format: reading}
      - {idx: 1, name: end,   kind: handwritten_number, format: reading}
      - {idx: 2, name: total, kind: handwritten_number, format: reading}
    rows: [{row: 0, key: reading}]
fields:
  - {name: equipment, kind: handwritten_text, bbox: [190, 105, 620, 185], meta_key: equipment}
  - {name: operator,  kind: handwritten_text, bbox: [330, 250, 800, 315], meta_key: operator}
  - {name: signature, kind: signature, bbox: [1806, 246, 2194, 314]}
```

계기 칸은 낮고, 계기 값은 길다(1234.5). 이웃한 두 칸의 값이 칸 폭의 절반보다 가까우면 덩어리 배정이 둘을 한 덩어리(메모)로 묶는다 —
그런 칸은 빈 칸으로 넘어가지 않고 검수 대기로 간다(ADR 0016). 실제 양식에서 `template preview --scan` 으로 칸과 글씨를 같이 본다.

## labels/pages.json

인식기가 아직 읽지 못하는 페이지 메타를 사람이 붙여 둔 것이다. 인식기가 생기면 필요 없어진다.

```json
{
  "<파일명(확장자 제외)>":          {"date": "2030-01-07"},
  "<파일명(확장자 제외)>#<페이지>":  {"vehicle_no": "V-101", "operator": "ALPHA"}
}
```

우선순위: 검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙 > 기계 값. 차량번호·작성자는 이제 검수 화면(`--queue page-fields`)으로 넣는 것이
기본이고(메타 필드 모델이 있으면 기계가 채운다), 라벨은 날짜를 고치거나 검수 전에 임시로 줄 때 쓴다. 라벨이 있는 쪽에서 기계가 다른 값을
읽으면 `mismatch` 로 남는다 — `review serve --queue meta-check` 에서 종이를 보고 정한다 (라벨이 틀렸으면 라벨을 고친다).

## reviews/reviews.jsonl

검수 화면(`minedocscan review serve`)이 쓰는 **사람이 입력한 값의 원본**이다. 한 줄 = 검수 한 건, 고치는 것도 새 줄을 추가한다.
지우거나 손으로 편집하지 않는다. DB(`doc_review`)는 이 파일의 사본이라 `WORK_ROOT` 를 지우고 다시 돌려도 값이 다시 붙는다.
사이트 팩에 두는 이유는 사이트 팩이 지워지지 않는 곳이기 때문이다. 공유 드라이브에 있어도 되지만 **한 번에 한 사람만 입력한다**
(동시에 쓰면 동기화 충돌이 난다). 다른 위치에 두려면 설정 `[paths] reviews` 또는 `MINEDOCSCAN_REVIEWS`.

```json
{"review_id": "…", "field_id": "ab12…-p2:haul:trips_day:3", "verdict": "value", "value": "7",
 "reviewer": "jp", "reviewed_at": "2030-01-08T01:02:03Z", "note": "",
 "source": "scan_2030-01-07#2", "template": "synth_haul_log", "region": "haul", "field_name": "trips_day",
 "row_no": 3, "row_key": "ORE|L3", "bbox": [604, 694, 846, 766],
 "machine": {"has_value": 1, "value_raw": "", "backend": "null", "confidence": 0.0}}
```

`verdict` 는 `value`(적힌 값), `empty`(빈 칸), `illegible`(읽을 수 없음). `source` 부터는 DB 없이도 읽을 수 있게 하는 문맥이고,
`machine` 은 검수 당시 기계가 낸 값이다. 템플릿을 고쳐 `bbox` 가 달라진 기록은 `review stats` 가 건수를 보여 준다 (적용은 한다).
실제 값(이름·차량번호)이 들어가므로 저장소에 넣지 않는다.

## models/<이름>/

`minedocscan recognizer train --crops … --name <이름>` 이 만든다 (같은 이름이 있으면 멈춘다 — 덮어쓰지 않는다). 현장의 손글씨로 학습한 것이므로
현장의 데이터다. **저장소에 넣지 않는다** (ADR 0011).

- `model.onnx` — 추론에 쓰는 망. OpenCV(`cv2.dnn`)로 읽는다. 배치 1 고정.
- `card.json` — 모델 카드: 이름, 만든 때, 코드 버전, 크롭 규격(`spec` — `digits` 백엔드가 그대로 선언한다), 입력 크기, 문자, 구조와 학습 인자, 씨앗,
  학습 데이터 요약(분할별 셀 수·날짜 수·값별 개수·합성 셀 수), 검증 성적(OpenCV 로 다시 읽어 잰 것), 온도, 자동 적재 기준과 임계값별 표,
  라이브러리 버전, `model.onnx` 의 SHA-256 (다르면 백엔드가 받지 않는다). 이미지·이름·차량번호·검수자는 적지 않는다.
- `train-log.jsonl` — 스텝별 손실·검증 정확도 (값은 없다).

`minedocscan recognizer list` 가 모델과 카드 요약을 보여 준다. 고르는 법은 설정 `[recognize.by_kind] handwritten_number = "digits"` +
`[recognize.digits] model = "<이름>"` (또는 위의 `site.toml` 항목). 모델이 없으면 `run` 이 시작할 때 멈춘다.

**메타 필드 모델** (`recognizer train --meta-key …`, tasks/0004). 같은 폴더 구조에 `classes.json` 이 더 있다 — 그 모델이 고를 수 있는 값의
목록이다. **이름과 차량번호가 들어 있으므로 사이트 팩 밖으로 내보내지 않는다.** 카드·학습 로그에는 종류의 수와 종류별 개수의 분포만 적는다.

```json
{"reader": "digits", "keys": ["vehicle_no"], "values": {"vehicle_no": ["4127", "4135", …]}}     // 숫자: 읽고 목록에서 고른다
{"reader": "choice", "keys": ["operator"], "classes": ["ALPHA", "BRAVO", …]}                   // 이름: 분류기 (종류 0 = "그 밖")
```

고르는 법은 설정(또는 `site.toml`)의 `[recognize.meta]` — 키마다 모델 이름. 한 모델이 여러 키를 읽을 수 있다(카드의 `meta.keys`).
카드에 없는 키에 꽂으면 `run` 이 시작할 때 멈춘다. `minedocscan info` 가 키마다 모델·읽는 법·자동 적재 기준(상한)을 보여 준다.

```toml
[recognize.meta]
vehicle_no   = "veh-v1"
operator     = "op-v1"
"date.month" = "date-v1"        # 점이 든 키는 따옴표로 (date.month = … 라고 써도 된다)
"date.day"   = "date-v1"
```

후보 목록은 `classes.json` 과 템플릿의 `header_<키>`(행렬 머리글)에서만 온다. 검수로 새 차·새 사람이 들어와도 다시 학습하기 전에는
기계가 고르지 않는다 — 그 쪽은 "목록에 없는 값"이나 기준 미만으로 남아 검수로 간다.

## 새 양식을 추가하는 절차

1. **기준 이미지 고르기** — 빈 양식이 가장 좋다. 없으면 글씨가 적고 반듯하게 스캔된 한 장.
2. **뼈대 만들기**
   ```bash
   minedocscan template init <이미지 또는 PDF> --name <이름> --page 3 --roi x0,y0,x1,y1 --header-rows 2
   ```
   괘선을 검출해 `templates/<이름>/template.yaml` 과 `reference.png` 를 쓴다. `--roi` 는 표 하나의 영역(200 dpi 픽셀)이다.
   표가 여러 개면 표마다 실행해 `regions` 를 합친다. 셀 정의 없이 두면(regions 가 빈 채) **분류 전용**으로 동작한다 — 그것만으로도
   묶음 PDF 에서 그 양식을 골라내고 통계를 낼 수 있다.
3. **열과 행 채우기** — 열의 `name`·`kind`, 행의 `key` 와 메타. 인쇄된 값은 행 메타에 그대로 옮겨 적는다.
4. **핸들러 고르기** — 기존 핸들러로 표현되면 `handler` 와 `handler_options` 만 적는다. 새 종류의 기록이면 핸들러를 만든다 ([ARCHITECTURE.md](ARCHITECTURE.md) §10).
5. **확인**
   ```bash
   minedocscan template check  <site>/templates/<이름>            # 오류를 전부: 읽기 오류, 겹치는 칸, 쪽 밖의 칸, 역할에 필요한 칸,
                                                                  #   형식과 종류의 불일치, 열·행 이름 겹침 … (있으면 종료 코드 1)
   minedocscan template preview <site>/templates/<이름>           # 칸·필드의 테두리·이름·종류·형식·역할·행 번호를 기준 이미지 위에
   minedocscan template preview <site>/templates/<이름> --scan <PDF> --page 3   # 그 쪽을 정합한 위에 — 칸이 글씨에 맞는지
                                                                  #   → WORK_ROOT/template-preview/*.png (저장소 안에는 쓰지 않는다)
   minedocscan info                         # 템플릿이 읽히는지, 셀 수가 맞는지
   minedocscan run <그 양식이 든 파일> --fresh
   minedocscan report                       # 정합 통과 수, 괘선 오차
   ```
   미리보기에는 실제 양식(머리글의 이름, --scan 이면 손글씨)이 보인다 — 문서·PR·이슈에 붙이지 않는다.
   정합 이미지는 `WORK_ROOT/aligned/<문서 ID>/pNN_<템플릿>.png` 에 있다. 괘선이 템플릿 좌표와 겹치는지 눈으로 본다.
6. **기준 갱신** — `minedocscan regress --update` 로 새 양식을 포함한 수치를 기준으로 저장한다.

## 개정판(같은 양식의 새 판)을 추가하는 절차

`report --by-month` 에서 어느 달부터 어떤 양식의 정합 수치(괘선 오차·인라이어)가 나빠지거나 배차 관측의 "머리글과 다름"이 늘면 그 달에 양식이 개정된 것이다.

1. 새 판의 깨끗한 쪽에서 기준 이미지를 다시 뜬다: `minedocscan template init <그 쪽> --name <이름>_v2 --roi …`. 열·행 메타(머리글의 운전자·차량번호)를 새 판대로 적는다.
2. 두 템플릿에 같은 `family` 를 적고, **옛 판에 `valid_to`(마지막 날), 새 판에 `valid_from`(첫 날)** 을 적는다. 겹치면 사이트 팩을 읽을 때 오류다.
3. `minedocscan info` 로 계열과 기간을 확인하고, 그 기간의 파일을 `run` 해 `report --by-month` 가 두 판으로 갈라지는지 본다.
4. `regress --update`. 왜 바뀌었는지 커밋 메시지에 적는다.

날짜를 모르는 쪽(파일명 규칙에 안 맞는 파일)은 모든 판이 후보라 틀린 판으로 갈 수 있다. 그런 파일은 문서 라벨로 날짜를 준다.

### 손봐야 할 때

- 분류가 다른 양식과 헷갈린다 (`low_margin`) → 서식이 같고 제목만 다른 양식은 한 템플릿으로 합치고 제목 필드로 구분한다.
- 정합은 되는데 괘선 오차가 크다 → 기준 이미지가 기울었거나 `grid` 가 실제 괘선과 어긋나 있다. `template init` 을 다시 돌려 좌표를 비교한다.
- 가로 양식에서 괘선이 덜 잡힌다 → 페이지 전체가 아니라 `--roi` 로 표 하나만 지정한다.
