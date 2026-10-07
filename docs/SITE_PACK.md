# 사이트 팩

사이트 팩은 한 현장의 양식 정의·옵션·라벨 묶음이다. 소프트웨어는 현장을 모르고, 현장에 관한 것은 전부 여기서 읽는다.
**사이트 팩은 저장소에 넣지 않는다** — 기준 이미지와 템플릿 머리글에 이름·서명·차량번호가 들어 있다 ([DATA.md](DATA.md)).

합성 사이트 팩을 만들어 보면 형식을 바로 볼 수 있다: `minedocscan synth out/demo` → `out/demo/site/`.
가동 일보는 `--usage-only`(인쇄 층까지 `--print-layers`, 같은 날 섞인 판까지 `--usage-variants`).
템플릿 도구 `template print-layer`·`add-region`·`variant`·`preview` 는 git 작업 트리 안에 쓰지 않는다 (현장 양식과 글씨다) — 그 명령을
합성 양식으로 해 보려면 합성 팩과 WORK_ROOT 를 저장소 밖 경로에 만든다 (아래 절차의 예).

```
<site>/
  site.toml                        현장 이름, 파일명 규칙, 장비 구분 대응, 교차검증 옵션
  templates/<양식>/template.yaml   양식 정의
  templates/<양식>/reference.png   기준 이미지 (빈 양식 또는 깨끗한 스캔 한 장, 200 dpi)
  templates/<양식>/print.png       인쇄 층 (선택, `template print-layer` 가 쓴다): 손글씨가 빠진 빈 양식 — 손글씨의 잔상이 남을 수 있다
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
# 장비 ID 는 쪽을 적재할 때 정해진다 — 고친 뒤에는 그 쪽들을 다시 돌린다 (run --fresh 또는 그 문서). `info` 가 대응표의 해시를,
# `report` 가 지금의 대응표와 장비 ID 가 다른 가동 기록·작업량의 행 수를 한 줄로 알린다 (이름 없이)
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
# family: haul_matrix              # 선택: 같은 양식의 판 묶음. 개정판은 valid_from / valid_to (YYYY-MM-DD, 양 끝 포함) 로 가린다
# valid_to: 2030-01-07
# concurrent: true                 # 선택: 같은 날 섞여 쓰이는 판 — family 가 있을 때만 (아래 "같은 날 섞여 쓰이는 판")
reference_image: reference.png
# print_image: print.png           # 선택: 인쇄 층 (가동 일보의 role 표에만 쓰인다) — template print-layer 로 만든 뒤 사람이 적는다
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
| `handwritten_number` | 위와 같되 숫자로 해석한다. `haul` 핸들러는 글씨 덩어리 배정으로, `usage` 의 `meter`·`shifts`·`tally` 표는 덩어리 배정 **또는** 잉크 비율로 값 유무를 정한다 (인쇄 층이 있으면 인쇄를 뺀 그림으로). 형식이 `integer` 가 아니면 인식기에 보내지 않는다 |
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
  나눔 선은 인쇄된 괘선이 아니므로 `template add-region`·`template variant` 가 잡지 못한다 — **사람이 적는다** (`preview --print` 의 그림에서
  인쇄된 두 줄 사이의 y 를 읽는다. 합성 로우더는 괘선 사이의 가운데다).
- **칸 안의 인쇄("하단:", "대", "~", "시작:")는 떼어 내지 않고 그대로 둔다.** 인쇄된 글자가 값 칸 안에 들어가면 늘 잉크로 보인다. 글자를
  `split_xs` 로 떼어 `printed` 칸으로 두는 것으로는 숫자를 잡지 못한다 — 양옆에 인쇄가 있는 칸("하단: _ 대")에서 쓴 숫자가 양옆의 인쇄와,
  인쇄가 이웃 칸의 인쇄와 이어져 덩어리 배정이 줄 전체를 메모로 본다 (실제 로우더 작업일보에서 인쇄를 떼어 낸 뒤에도 두 자리 값 48개 중 0개).
  그래서 `usage` 의 role 표(`meter`·`shifts`·`tally`) 칸은 덩어리 배정 **또는** 잉크 비율 중 하나라도 "있음"이면 인식기·검수로 보낸다 — 값이
  빈 칸으로 사라지지 않는다. 그 대가로 **인쇄 층이 없으면** 인쇄만 있는 칸은 비어 있어도 늘 검수 대기다. 인쇄 층(`print_image`, 아래)을 켜면
  인쇄를 뺀 그림으로 재므로 그런 칸이 빈 칸으로 자동 적재된다 (실제 3일치로 미리 재 본 것: 작업량의 인쇄 칸 48개 중 "있음" 48 → 1,
  근무 시각의 빈 칸 9 → 0, 계기 칸의 빈 칸 60 중 5 → 2).

합성 예 (`minedocscan synth out/u --usage-only` 의 `synth_loader_log`, 줄임):

```yaml
handler: usage
# print_image: print.png           # --print-layers 로 만든 합성 팩에만
regions:
  - name: tally
    role: tally
    grid: {ys: [360, 420, 508, 596, …], xs: [150, 400, 560, 720, 880, 1040], split_ys: [464, 552, …]}   # split_ys 는 도구가 잡지 않는다 — 사람이 적는다
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
그런 칸은 빈 칸으로 넘어가지 않고 검수 대기로 간다(ADR 0016). 작업량 칸 안의 인쇄도 마찬가지다 — 인쇄 층이 없으면 (위). 실제 양식에서
`template preview --scan` 으로 칸과 글씨를, `template preview --print` 로 칸과 인쇄를 같이 본다. 가동 일보 템플릿을 처음부터 만드는 순서는
아래 "가동 일보 템플릿을 만드는 절차".

### 인쇄 층 (`print_image`)

선택 키. 그 양식에 **인쇄된 것만** 있는 그림 — 손글씨가 빠진 빈 양식 — 을 가리킨다 (ADR 0017). 빈 양식을 스캔해 올 필요가 없다:
`template print-layer` 가 그 양식으로 분류된 쪽들을 템플릿 좌표로 펴서 겹치고 화소마다 밝기의 높은 백분위를 잡는다. 인쇄는 모든 쪽의 같은
자리에 있고 손글씨는 쪽마다 다르기 때문이다.

```yaml
reference_image: reference.png
print_image: print.png             # 템플릿 폴더 안의 파일 이름
```

- 파일은 PNG 이고 기준 이미지와 같은 크기다 (같은 템플릿 좌표계). 값은 템플릿 폴더 바로 안의 파일 이름이다 — 폴더(`../<다른 판>/print.png`,
  `sub/print.png`)나 절대 경로는 템플릿 오류다 (판마다 자기 층). 파일이 없거나, 크기가 다르거나, 확장자가 `.png` 가 아니면 템플릿 오류다 —
  사이트 팩 전체가 읽히지 않는다. 그래서 **키는 `print-layer` 로 파일을 만든 뒤에 사람이 적는다** (명령은 `template.yaml` 을 고치지 않고 적을 줄을 안내한다).
- 쓰는 곳은 하나다: role 이 `meter`·`shifts`·`tally` 인 표의 형식 있는 손글씨 칸의 **값 유무**. 정합 그림을 이진화한 뒤 인쇄 화소(인쇄 층을 같은
  방법으로 이진화해 2 px 넓힌 것)를 지운 그림으로 그 칸의 잉크 비율과 덩어리 배정을 잰다. 규칙과 임계값은 그대로다.
- 쓰지 않는 곳: 표 밖 필드(서명·체크·메타 필드 포함), 작업 표(`activities`), 인쇄된 칸, role 이 없는 표(운반·점검표), ✓ 판정, 정합·분류,
  크롭 — 인식기·내보내기·검수 화면은 인쇄가 있는 원래 그림을 본다. 날마다 같은 자리에 같은 글씨로 쓰는 필드(작성자, 늘 같은 점검란, 서명)는
  쪽이 많아도 인쇄 층에 들어가 지워지기 때문이다. 그런 칸만 있는 양식에 키를 적어도 값 유무는 그대로다 (`template check` 가 참고로, `info` 가 알린다).
- 기준 이미지를 대신하지 않는다 — 정합·분류의 기준은 지금처럼 `reference.png`(스캔 한 장)다. 다만 **괘선은 인쇄 층에서 잡는 것이 낫다**:
  `template add-region` 은 `print_image` 가 있으면 그것에서 잡는다 (아래 절차).
- 판(`concurrent`, 아래)마다 따로다. `template variant` 는 복사하지 않는다.
- 어느 층으로 쟀는지 쪽마다 남는다 (`doc_page.print_sha` — 화소의 해시, `info` 도 템플릿마다 보여 준다). 층을 다시 만들면 해시가 바뀐다 →
  `run --fresh` (`--skip-existing` 은 같은 파일을 건너뛰어 다시 재지 않는다).
- **저장소에 넣지 않는다.** 날마다 같은 자리에 같은 글씨로 쓰는 칸(작성자 이름, 서명, 같은 자리의 계기 값)은 쪽이 많아도 잔상이 남는다 — 현장
  글씨다 ([DATA.md](DATA.md)).

```bash
minedocscan template print-layer <site>/templates/<양식> [--max-pages 40] [--percentile 75] [--out FILE]
```

- 쪽: 그 템플릿으로 분류된 쪽(`loaded`·`classified_only`)을 날짜별로 돌아가며 최대 `--max-pages` 장. WORK_ROOT 의 정합 그림이 없으면 원본을
  다시 펴고, **표가 없는 분류 전용 템플릿의 쪽은 명령이 원본에서 직접 정합한다** (인라이어가 60 이 안 되는 쪽은 뺀다). 원본 스캔이 있어야 한다.
  DB 는 읽기만 한다.
- **5장 이상으로 만든다.** 3장 미만이면 거절하고, 5장 미만이면 경고하고 만든다. 쪽이 적으면 여러 쪽의 같은 자리에 쓴 값이 잔상으로 남아
  그 자리에 쓴 값까지 지울 수 있다 — 합성에서 2장으로 만든 로우더 층이 값이 적힌 작업량 칸 3칸을 잃었다 (백분위를 보간 없이 잡아 3–5장은
  잃지 않았다. 보간하던 때는 3장도 4칸을 잃었다).
  쪽이 모자라면 키를 아직 적지 않는다 — 인쇄 층이 없어도 값은 사라지지 않는다 (인쇄가 든 칸이 검수로 갈 뿐이다).
- `--percentile`: 기본 75, 보간 없이 (그 자리 위의 실제 쪽 값). **판이 섞였을 수 있는 양식의 첫 층은 50** — 판을 나누기 전에는 두 판의 쪽이
  한 층에 들어가고, 소수 판의 몫이 25 % 를 넘으면 75 백분위에서는 다수 판의 표 괘선이 층에서 빠진다 (`add-region` 이 괘선을 못 잡는다).
  - **50 의 층은 괘선을 잡는 데(`add-region`)만 쓴다.** 값 유무에 쓰는 층(`print_image` 를 적고 `run`)은 판을 나눈 뒤 판마다 **75 로 다시
    만든다** — 낮은 백분위의 층에는 같은 자리에 쓴 값의 잔상이 더 남아 그 값을 빈 칸으로 만든다. 75 미만으로 만들면 요약이 경고하고,
    `print_image` 안내도 "add-region 이 괘선을 잡게 하려면"으로 바뀐다. 50 의 층을 `print_image` 로 둔 채 `run` 하지 않는다 (아래 순서).
  - 보간하지 않으므로 50 의 층은 한 판이 쪽의 **절반을 넘을 때만** 그 판의 괘선을 남긴다 — 두 판이 꼭 반씩이면(2+2, 5+5) 둘 다 빠진다.
    요약의 괘선 수가 0 에 가까우면 `--max-pages` 를 홀수로 해 다시 만든다.
- 출력: 기본 `<템플릿 폴더>/print.png` (`--out` 도 PNG 만). git 작업 트리 안이면 거절한다 (합성 팩이면 `--allow-in-repo`).
- 요약 (값·이름 없이): 쓴 쪽·날짜의 수, 얻은 법(`aligned` 저장된 정합 그림 / `rewarped` 다시 편 것 / `aligned_now` 직접 정합한 것), 뺀 쪽의 이유별 수,
  백분위, 인쇄 화소의 비율, `print_sha`, **인쇄에 덮인 손글씨 칸**(큰 순서로 10개 — `<표>/<열>/행 N`, `fields/<이름>`). 표 칸이 덮였으면 칸 안의
  인쇄이거나 잔상이다 — `template preview --print` 로 본다. 필드의 잔상은 상관없다 (필드는 인쇄 층을 쓰지 않는다).
- 확인: `template check` 는 인쇄 층의 파일·크기와, 표의 손으로 쓰는 칸 중 인쇄 마스크가 절반 넘게 덮은 칸(값이 들어갈 자리가 없다)을 오류로
  알린다. `template preview <폴더> --print` 는 인쇄 층 위에 인쇄 화소를 청록으로 칠하고 칸을 그린다 (`WORK_ROOT/template-preview/<양식>__print.png`,
  `--scan` 과 같이 쓰지 않는다).

### 같은 날 섞여 쓰이는 판 (`concurrent`)

묵은 양식을 같이 쓰면 **같은 날 묶음에 인쇄 판 두 개가 섞인다** — 머리·제목은 같은 자리이고 표만 몇 px 아래에 있거나 줄 간격이 조금 다른 판
(실제 중기운행일보: 30쪽 중 5쪽이 표가 8–12 px 아래인 판 B). 날짜로 가리는 개정판(`valid_from`·`valid_to`)으로는 안 되고, 모양(분류 점수)으로도
가르지 못한다. 판마다 별도 템플릿을 두고 **두 판 모두에** 같은 `family` 와 `concurrent: true` 를 적는다 (ADR 0018).

```yaml
name: synth_usage_log_b            # 판 B — 판 A(synth_usage_log)에도 아래 두 줄
family: synth_usage_log
concurrent: true
```

- `concurrent` 는 `true`/`false` 이고 `family` 가 있을 때만 쓴다 (없으면 템플릿 오류).
- 같은 계열에서 유효 기간이 겹치면 오류인 규칙(개정판)은 그대로이고, **겹치는 두 판 모두에** `concurrent: true` 가 있을 때만 허용한다. 한쪽에만
  있으면 겹침 오류다. 동시 판에도 `valid_from`·`valid_to` 를 적을 수 있다 (두 판의 유효 기간이 겹치지 않아도 읽힌다).
- 계열에 `concurrent: true` 인 판이 **하나뿐이면** 오류다 — `template variant` 가 새 판에만 적고 기존 판에 적을 두 줄을 안내한 채로 남은
  상태다. 오류가 그 판과 계열, 두 줄(`family: <계열>`, `concurrent: true`)을 적을 판(계열과 이름이 같은 템플릿)을 말한다.
- 사이트 팩 안에서 템플릿의 `name` 은 하나다 — 이름이 같은 템플릿이 둘이면(폴더 이름이 달라도) 오류다.
- **동시 판끼리는 기하 밖의 모든 것이 같아야 한다**: `handler`·`handler_options`, 표(이름·`role`·`header_rows`·열·행 — 열·행의 메타
  `shift`·`subtotal`·`item`·`place`·`header_*` … 까지 통째로), 필드(`bbox` 밖 전부). `format` 을 적지 않은 칸은 그 종류의 기본 형식으로
  비교한다. 다를 수 있는 것은 괘선 좌표(`grid` — 나눔 선 포함)·필드의 `bbox`·기준 이미지·인쇄 층·이름·제목·유효 기간뿐이다. 다르면 사이트 팩을
  읽을 때 오류다 (표 이름과 항목의 종류만 알린다 — 행 키·값은 찍지 않는다). 검수 기록이 칸을 찾는 `field_id` 에는 템플릿 이름이 없다 — 키가
  같아야 판이 바뀐 쪽에도 검수가 붙고, 메타·`header_rows` 가 같아야 같은 `field_id` 가 판마다 같은 뜻의 칸을 가리킨다. `template variant` 는
  그대로 복사한다. 한 판을 손으로 고치면 다른 판도 같이 고친다.
- `template check` 는 템플릿 하나만 본다. 판끼리의 키는 사이트 팩을 읽을 때(`info`, `run`) 드러난다.
- 묶이지 않은 두 판은 분류가 모양으로 판 하나를 골라 그 판에만 정합한다 — 틀린 판의 괘선 오차가 정합 기준(6 px) 안이면 칸이 어긋난 채
  조용히 적재된다. 그래서 `concurrent` 를 한 판에만 적거나, `family` 를 한 판에만(또는 서로 다르게) 적으면 사이트 팩이 읽히지 않는다 (위의
  겹침 오류, 또는 계열에 동시 판이 하나뿐). `info` 에서 두 판이 같은 계열의 "같은 날 섞여 쓰이는 판"으로 나오는지 본다.
- 처리: 분류는 그날 유효한 같은 계열의 동시 판들을 한 후보로 묶는다. 묶음이 이기면 **판마다 정합해, 통과한 판 중 괘선 오차가 가장 작은 판**을
  고른다 (차이가 0.5 px 안이면 인라이어가 많은 판, 그다음 이름 순). 통과한 판이 없으면 `align_failed`. 쪽의 `template_name` 은 고른 판,
  `variant_errs` 는 판마다의 괘선 오차다. 판이 하나뿐인 양식은 지금처럼 한 번만 정합한다. `run --template` 으로 양식을 정하면 묶지 않는다.
- `eval` 과 oracle 은 사이트 팩이 있으면 동시 판의 정답을 계열로 맞춘다 — 정답의 `template` 이 어느 판의 이름이어도 같은 쪽에 붙는다.
- 리포트: `report` 의 "동시 판 <계열>: 고른 쪽 …, 정합 실패 N, 고른 쪽 중 두 판의 괘선 오차 차이가 1 px 미만인 쪽 N" (두 판 모두 정합에
  실패한 쪽은 정합 실패로만 센다). 가르기 어려웠던 쪽은 `pages --variants` (정합 실패 쪽도 상태와 함께 나온다).

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
   괘선을 검출해 `templates/<이름>/template.yaml` 과 `reference.png` 를 쓴다. `--roi` 는 표 하나의 영역(200 dpi 픽셀)이다 (표 이름은 `main`).
   `--roi` 가 없으면 쪽 전체에서 잡은 표 하나를 쓰거나(표가 여럿인 쪽에서는 틀린 표가 된다 — 지운다), 잡지 못하면 `regions: []` 로 둔다.
   표가 여러 개면 나머지 표는 `template add-region` 으로 하나씩 더한다 (아래 "가동 일보 템플릿을 만드는 절차" 4).
   셀 정의 없이 두면(`regions: []`) **분류 전용**으로 동작한다 — 그것만으로도 묶음 PDF 에서 그 양식을 골라내고 통계를 낼 수 있다
   (쪽은 `classified_only`). 가동 일보는 이 상태에서 인쇄 층부터 만든다 (아래).
3. **열과 행 채우기** — 열의 `name`·`kind`, 행의 `key` 와 메타. 인쇄된 값은 행 메타에 그대로 옮겨 적는다.
4. **핸들러 고르기** — 기존 핸들러로 표현되면 `handler` 와 `handler_options` 만 적는다. 새 종류의 기록이면 핸들러를 만든다 ([ARCHITECTURE.md](ARCHITECTURE.md) §10).
5. **확인**
   ```bash
   minedocscan template check  <site>/templates/<이름>            # 오류를 전부: 읽기 오류, 겹치는 칸, 쪽 밖의 칸, 역할에 필요한 칸,
                                                                  #   형식과 종류의 불일치, 열·행 이름 겹침 … (있으면 종료 코드 1)
   minedocscan template preview <site>/templates/<이름>           # 칸·필드의 테두리·이름·종류·형식·역할·행 번호를 기준 이미지 위에
   minedocscan template preview <site>/templates/<이름> --scan <PDF> --page 3   # 그 쪽을 정합한 위에 — 칸이 글씨에 맞는지
   minedocscan template preview <site>/templates/<이름> --print   # 인쇄 층(print_image) 위에 — 값 자리가 인쇄에 덮이지 않았나
                                                                  #   → WORK_ROOT/template-preview/*.png (저장소 안에는 쓰지 않는다)
   minedocscan info                         # 템플릿이 읽히는지, 셀 수가 맞는지
   minedocscan run <그 양식이 든 파일> --fresh
   minedocscan report                       # 정합 통과 수, 괘선 오차
   ```
   미리보기에는 실제 양식(머리글의 이름, --scan 이면 손글씨)이 보인다 — 문서·PR·이슈에 붙이지 않는다.
   정합 이미지는 `WORK_ROOT/aligned/<문서 ID>/pNN_<템플릿>.png` 에 있다. 괘선이 템플릿 좌표와 겹치는지 눈으로 본다.
6. **기준 갱신** — `minedocscan regress --update` 로 새 양식을 포함한 수치를 기준으로 저장한다.

## 가동 일보 템플릿을 만드는 절차

가동 일보(`handler: usage` — 중기운행일보·점보 작업일보·로우더 작업일보)는 칸 안에 인쇄가 있고("하단: _ 대", "~", "시작:"), 빈 양식이 없어
기준 이미지로 채워진 스캔을 쓴다. 채워진 스캔에서 괘선을 잡으면 손글씨의 세로획이 괘선으로 섞인다. 그래서 칸을 정하기 전에 **분류 전용으로
먼저 돌려 인쇄 층을 만들고, 그 위에서 표를 잡는다** (tasks/0006 8절). 실데이터에서 할 일의 순서와 입력은 [DATA.md](DATA.md) "장비 가동 일보".

**열·행을 채우고 `template check` 가 통과할 때까지 그 사이트 팩으로 `run` 하지 않는다** — 템플릿 오류 하나로 사이트 팩 전체가 멈춘다.

1. **분류 전용 템플릿** — 기준 이미지가 될 쪽 하나(글씨가 적고 반듯한 쪽)로 뼈대를 만든다.
   ```bash
   minedocscan template init <PDF> --page N --name <양식> --handler usage
   ```
   `--roi` 없이 쪽 전체에서 표(`main`)를 잡았으면 지우고 `regions: []` 로 둔다 (`fields: []` 는 그대로). 이 템플릿의 쪽은 분류만 된다 —
   `classified_only`, 정합도 칸도 없다. 이 상태로 `template check` 는 통과한다.
2. **분류** — 전체 묶음을 돌린다: `minedocscan run DB_scans`.
3. **인쇄 층**
   ```bash
   minedocscan template print-layer <site>/templates/<양식>          # 묵은 판이 섞여 쓰이는 양식이면 --percentile 50 (괘선을 잡는 데만)
   ```
   분류 전용 쪽은 명령이 원본에서 직접 정합한다 — 칸 정의가 없어도 만든다. 5장 이상으로 (위 "인쇄 층"). 요약을 본 뒤 `template.yaml` 에
   `print_image: print.png` 를 적는다 (명령이 안내한다). `--percentile 50` 으로 만든 층이면 표를 다 더한 뒤(4번) 그 줄을 지우고 돌린다 —
   값 유무에 쓰는 층은 판을 나눈 뒤 판마다 75 로 다시 만든다 (아래 "같은 날 섞여 쓰이는 판" 5번). `template preview <폴더> --print` 의 그림은 줄이지 않은 템플릿 좌표 그대로다 —
   다음 단계의 `--roi`, 나눔 선의 y, 필드의 `bbox` 를 이 그림의 픽셀에서 읽는다.
4. **표 더하기** — 표마다 한 번:
   ```bash
   minedocscan template add-region <site>/templates/<양식> --roi x0,y0,x1,y1 --name <표> [--role meter|shifts|tally|activities] [--header-rows N]
   ```
   - 괘선은 `print_image` 가 있으면 인쇄 층에서, 없으면 기준 이미지에서 잡는다 (요약에 어느 쪽인지 나온다). 그래서 `print_image` 를 적은 뒤에
     한다 — 합성 로우더의 채워진 쪽에서 잡으면 계기 값이 계기 표에 없는 세로 괘선을 하나 더 만들었고, 인쇄 층에서는 생성기의 괘선과 같았다.
   - `--roi` 는 템플릿 좌표다. **표 둘레를 조금 넉넉히, 이웃 표까지의 간격보다는 좁게** 잡는다 (합성 두 양식은 표 둘레 20 px 에서 맞는다. 표 사이가
     50 px 인 합성 운행일보에서는 60 px 이면 이웃 표의 괘선이 들어온다).
   - **요약의 괘선 수(가로·세로)를 인쇄된 표와 맞춰 본다.** 많으면 잔상이다 — 쪽이 적은 층에서는 같은 자리에 쓴 값이 괘선으로 잡힐 수 있다
     (백분위를 보간하던 때 합성 운행일보 4쪽 층: 계기 표의 시작 칸 안에 세로 괘선 하나. 보간 없이는 3·4쪽 층도 맞다). 영역을 넓혀서는 풀리지
     않는다 — 쪽이 쌓인 뒤 층을 다시 만들어 잡거나, 남는 괘선을 `grid` 에서 지운다. 적으면 판이 반씩 섞인 50 의 층일 수 있다 (위).
   - 표는 `regions` 블록의 끝(다음 최상위 키 바로 앞)에 글자로 끼워 넣는다. 다른 키와 주석은 그대로이고 `regions: []` 는 블록이 된다. 쓴 뒤
     다시 읽어 표 하나만 늘었는지 보고, 아니면 원래 파일로 되돌린다. 템플릿은 검증 없이 읽는다 — 채우는 중의 템플릿에도 더할 수 있다.
   - 거절 (한 줄, 파일은 그대로): 같은 이름의 표, 이름 `fields`, 알 수 없는 `--role`, 쪽 밖이거나 빈 영역, 괘선이 모자람, `print_image` 의 파일이
     없거나 크기가 다름, 흐름 형식(`regions: [ … ]`)이나 목록이 아닌 `regions` (블록 형식으로 바꾼 뒤 다시), git 작업 트리 안의 템플릿
     (합성 팩이면 `--allow-in-repo`).
   - `--role` 을 주면 그 역할이 요구하는 열의 자리표시로 뼈대를 만든다. 행 키는 `row_<i>` 다 (`template init` 과 같은 뼈대). handler 가 `usage` 가
     아닌 템플릿에 주면 표는 쓰고 알린다 (`template check` 가 오류로 낸다).

     | `--role` | 자리표시 | 사람이 채울 것 |
     |---|---|---|
     | (없음) | 열 전부 `col_<i>`(`handwritten_text`) | 전부 |
     | `meter` | 뒤의 열 셋이 `start`·`end`·`total`(`handwritten_number`, `reading`), 앞에 열이 더 있으면 인쇄된 이름 칸(`col_<i>`, `printed`). 열이 둘이면 `start`·`end`. 열이 하나이고 행이 둘 이상이면 세로 표 — 행 키 `start`·`end`·`total`, 열 `reading` | 자리표시가 시작·종료·총의 인쇄된 머리에 붙었는지 (아래) |
     | `shifts` | 마지막 열 `range`(`handwritten_number`, `time_range`). 열이 둘 이상이면 첫 열 `shift`(`printed`) | 행 키(`am`·`pm`·`ot` …)와 행 메타 `shift`(인쇄된 근무 구분) |
     | `tally` | 열 전부 `handwritten_number`, `integer` | 구분·장소 열을 `printed` 로, 행 키와 행 메타 `item`·`place`, 열 메타 `shift`, 소계 칸 `subtotal: true`, 나눔 선 `split_ys` |
     | `activities` | 열 전부 `handwritten_text` | 번호 열 `printed`, 합계 줄 `subtotal: true` |

     요약이 자리표시가 붙은 열을 적는다 (`열 1 start handwritten_number/reading` …). **`template check` 는 자리표시가 맞는 열에 붙었는지 보지
     않는다** — 요약의 자리를 `preview --print` 의 인쇄된 머리와 맞춰 본다.
   - 그 템플릿이 동시 판(`concurrent`)이면 요약이 알린다: 같은 계열의 다른 동시 판에도 같은 표(같은 `--name`·`--role`·`--header-rows`, 같은 열·행과 그 메타)를 더해야
     사이트 팩이 읽힌다. 영역은 판마다 그 판의 표 자리로 준다.
5. **채우기** — 열 `name`·`kind`·`format`, 행 `key` 와 메타, 필드(장비명 `meta_key: equipment`, 운전자 `meta_key: operator`, 서명
   `kind: signature`, 연료·오일·특이사항 — 위 "핸들러별 약속"). 칸 안의 인쇄된 줄로 행을 나누는 선(`split_ys`·`split_xs`)은 잡히지 않으므로
   사람이 적고, 그만큼 `rows` 를 늘린다 (`row` 는 괘선과 나눔 선을 합친 순서). 인쇄가 든 칸은 떼어 내지 않는다.
6. **확인**
   ```bash
   minedocscan template check   <site>/templates/<양식>             # "오류 없음" 일 때까지
   minedocscan template preview <site>/templates/<양식> --print     # 칸이 인쇄된 표에 맞는지, 값 자리가 인쇄에 덮이지 않았나
   minedocscan template preview <site>/templates/<양식> --scan <PDF> --page N   # 칸이 실제 글씨에 맞는지
   minedocscan info                                                # 표·셀 수, 인쇄 층과 그 해시
   ```
7. **돌리기** — `minedocscan run DB_scans --fresh` → `minedocscan report` ("인쇄 층으로 값 유무를 잰 쪽: <양식> N"). 분류 전용 쪽을 직접 정합해 만든
   층도 같은 템플릿 좌표라 다시 만들 필요는 없다 (합성 로우더: 적재된 쪽으로 다시 만들어도 해시까지 같았다). 그 뒤 `regress --update`.
8. **다른 판** — 몇 쪽만 `align_failed` 이거나 괘선 오차가 크게 떨어져 있으면 아래 "같은 날 섞여 쓰이는 판을 추가하는 절차".

**합성 양식으로 해 보기.** 저장소 밖 경로에서 한다 — `out/` 은 저장소 안이라 `print-layer`·`add-region`·`preview` 가 거절한다.
합성 로우더 일보(`synth_loader_log` — 작업량·근무 시각·계기 표 셋)를 분류 전용으로 돌려 표를 다시 만든다:

```bash
minedocscan synth /tmp/demo --usage-only --days 10
cp -r /tmp/demo/site /tmp/demo/site2
# site2/templates/synth_loader_log/template.yaml 의 regions 와 fields 를 비운다: regions: [] , fields: []
#   (점검표 synth_inspection 은 둔다 — site.toml 의 [equipment.aliases] 가 그 장비 행을 가리킨다)
export MINEDOCSCAN_SITE=/tmp/demo/site2 MINEDOCSCAN_ARCHIVE_ROOT=/tmp/demo/scans MINEDOCSCAN_WORK_ROOT=/tmp/demo/work
T=/tmp/demo/site2/templates/synth_loader_log
minedocscan run                                  # 로우더 20쪽이 classified_only
minedocscan template print-layer $T              # 쪽 20장, 날짜 10일, 얻은 법 aligned_now 20
# $T/template.yaml 에 print_image: print.png 를 적는다
minedocscan template add-region $T --roi 130,340,1060,1056 --name tally  --role tally
minedocscan template add-region $T --roi 1180,340,1860,620 --name shifts --role shifts
minedocscan template add-region $T --roi 1180,680,2330,808 --name meter  --role meter
minedocscan template check $T                    # 자리표시만으로도 "오류 없음" — 아직 채운 것이 아니다
minedocscan template preview $T --print          # → /tmp/demo/work/template-preview/synth_loader_log__print.png
```

- 영역은 생성기의 표에 둘레 20 px 를 더한 것이다. 잡은 괘선(`grid.ys`·`xs`)은 원래 합성 템플릿(`/tmp/demo/site/templates/synth_loader_log/template.yaml`)과
  같다 — 채울 열·행·필드와 작업량 표의 `split_ys`(잡히지 않는다)도 거기서 옮겨 적는다. 채운 뒤 `template check` → `run --fresh` 하면 `report` 에
  "인쇄 층으로 값 유무를 잰 쪽: synth_loader_log 20" 이 나온다.
- 운행일보(`synth_usage_log`)도 같다: 작업 표 `--roi 80,500,1574,1020 --name work --role activities`, 계기 표
  `--roi 80,1030,1574,1158 --name meter --role meter` — 계기 표는 첫 열이 인쇄된 이름 칸이라 자리표시가 뒤의 세 열에 붙는다.

## 개정판(같은 양식의 새 판)을 추가하는 절차

`report --by-month` 에서 어느 달부터 어떤 양식의 정합 수치(괘선 오차·인라이어)가 나빠지거나 배차 관측의 "머리글과 다름"이 늘면 그 달에 양식이 개정된 것이다.

1. 새 판의 깨끗한 쪽에서 기준 이미지를 다시 뜬다: `minedocscan template init <그 쪽> --name <이름>_v2 --roi …`. 열·행 메타(머리글의 운전자·차량번호)를 새 판대로 적는다.
2. 두 템플릿에 같은 `family` 를 적고, **옛 판에 `valid_to`(마지막 날), 새 판에 `valid_from`(첫 날)** 을 적는다. 겹치면 사이트 팩을 읽을 때 오류다.
3. `minedocscan info` 로 계열과 기간을 확인하고, 그 기간의 파일을 `run` 해 `report --by-month` 가 두 판으로 갈라지는지 본다.
4. `regress --update`. 왜 바뀌었는지 커밋 메시지에 적는다.

날짜를 모르는 쪽(파일명 규칙에 안 맞는 파일)은 모든 판이 후보라 틀린 판으로 갈 수 있다. 그런 파일은 문서 라벨로 날짜를 준다.
같은 날 묶음에 두 판이 섞여 있으면 날짜로 가를 수 없다 — 아래 절차.

## 같은 날 섞여 쓰이는 판을 추가하는 절차

한 양식의 쪽 몇 장만 `align_failed` 이거나 괘선 오차가 다른 쪽보다 크게 떨어져 있으면 같은 날 섞여 쓰이는 다른 인쇄 판일 수 있다
(위 "같은 날 섞여 쓰이는 판"). 정합 기준(6 px) 안에서 통과한 쪽은 칸이 어긋난 채 적재되므로 `align_failed` 만 보지 말고 쪽마다의 괘선 오차도 본다.

1. **찾기**
   ```bash
   minedocscan pages --status align_failed --template <양식> --thumbs   # 실패한 쪽과 미리보기 (WORK_ROOT/thumbs)
   minedocscan pages --template <양식>                                 # 쪽마다 괘선 오차 — 몇 쪽만 크게 떨어져 있나
   minedocscan report --by-month                                       # 양식 × 월의 괘선 오차 (개정판이면 달로 갈린다)
   ```
2. **새 판의 템플릿** — 그 판의 쪽 중 머리·제목이 잘 보이는 깨끗한 쪽으로:
   ```bash
   minedocscan template variant <site>/templates/<양식> --scan <PDF> --page N --name <양식>_b [--out-dir DIR]
   ```
   - 그 쪽을 **표 영역(괘선 범위 + 60 px) 밖의 특징점**(머리·제목)으로 기존 판에 맞춰 편 그림이 새 판의 기준 이미지다. 표마다 기존 괘선을
     ±min(40 px, 이웃 표까지 간격의 절반) 안의 가장 가까운 괘선과 짝지어 **괘선만 다시 잡는다.** 붙은 표가 같이 쓰는 경계선(이 표의 괘선과
     3 px 안)은 간격에서 뺀다 — 작업 표와 계기 표가 붙어 있어도 그다음 괘선까지의 절반이 반경이다. 열·행·필드·`handler`·`role`·`format`·메타는
     그대로 복사하고, 표 밖 필드의 `bbox` 도 그대로 둔다 (머리가 같은 자리라는 가정 — 미리보기로 확인한다).
   - 새 판에는 `family`(기존 판의 것, 없으면 기존 판의 이름)와 `concurrent: true` 가 적힌다. 유효 기간은 복사하고 `print_image` 는 복사하지 않는다.
   - **기존 판의 파일은 고치지 않는다** — 요약이 기존 판에 적을 줄(`family: …`, `concurrent: true`)을 알려 준다. 적기 전에는 사이트 팩이
     읽히지 않는다 (계열에 동시 판이 하나뿐 — 오류가 적을 판을 말한다). `run` 전에 적는다.
   - 짝이 없는 괘선이 있는 표, 인쇄되지 않은 나눔 선(`split_ys`·`split_xs`)이 있는 표는 기존 괘선을 그대로 두고 "사람이 고칠 것"으로 알린다 —
     새 판의 그림을 보고 그 표의 `grid` 를 고쳐 적는다.
   - 거절 (한 줄): 기존 판에 템플릿 오류가 있거나 읽을 수 없음(YAML, `name` 없음, 기준 이미지 없음·깨짐 — `template check` 를 안내한다), 표가
     없음(분류 전용), 새 판의 이름이 기존 판과 같거나 옆 템플릿(기존 판의 `templates` 폴더, 출력 폴더의 부모)이 이미 씀, 표 밖 특징점의
     인라이어가 60 미만(머리·제목이 보이는 다른 쪽으로), 출력 폴더(기본: 기존 판 옆의 `<NAME>`)가 이미 있거나 git 작업 트리 안 (새 판의
     기준 이미지는 현장 스캔이다).
3. **확인** — `template check <양식>_b`, `template preview <양식>_b --scan <PDF> --page N` (표 밖 필드의 `bbox` 가 맞는지), `info` (두 판이 같은
   계열의 "같은 날 섞여 쓰이는 판"으로 나오는지 — 키가 다르면 여기서 오류).
4. **돌리기** — `run DB_scans --fresh` → `report` 의 "동시 판 <계열>: 고른 쪽 …, 정합 실패 N, 고른 쪽 중 두 판의 괘선 오차 차이가 1 px 미만인 쪽 N".
   가르기 어려웠던 쪽은 `pages --variants` (판마다의 괘선 오차를 같이 낸다).
5. **인쇄 층을 판마다** — B 로 적재된 쪽으로 `template print-layer <양식>_b` → B 의 `template.yaml` 에 `print_image: print.png`. 판을 나누기 전에
   만든 A 의 층(`--percentile 50`)에는 B 의 쪽이 섞여 있고 백분위도 낮다 — A 도 A 로 적재된 쪽만으로 **기본 백분위 75** 로 다시 만들고 키를
   다시 적는다 (요약에 75 미만 경고가 없어야 한다). 그리고 `run --fresh`.
   B 의 쪽이 5장이 안 되면 B 의 키는 아직 적지 않는다 (인쇄 층이 없는 판은 인쇄가 든 칸이 검수로 갈 뿐이다).
6. `regress --update` — 무엇이 왜 바뀌었는지 커밋 메시지에 적는다.

동시 판에 나중에 표를 더하거나 열·행을 고치면 계열의 모든 동시 판에 똑같이 한다 (위 "같은 날 섞여 쓰이는 판"의 키).

**합성으로 해 보기.** `--usage-variants` 의 합성 묶음에는 운행일보의 판 B(`synth_usage_log_b` — 작업 표·계기 표만 10 px 아래, 줄 간격 +1 %)가
날마다 섞여 있다. 판 A 만 있던 때로 되돌려 놓고 판 B 를 다시 만든다 (저장소 밖 경로에서 — `template variant` 는 저장소 안에 쓰지 않는다):

```bash
minedocscan synth /tmp/var --usage-only --usage-variants --days 10
# 판 A 만 남긴다: /tmp/var/site/templates/synth_usage_log_b 를 지우고, synth_usage_log/template.yaml 의 family·concurrent 두 줄을 지운다
export MINEDOCSCAN_SITE=/tmp/var/site MINEDOCSCAN_ARCHIVE_ROOT=/tmp/var/scans MINEDOCSCAN_WORK_ROOT=/tmp/var/work
minedocscan run
minedocscan pages --status align_failed --template synth_usage_log   # 판 B 의 쪽 15장 중 14–15장 (OpenCV 판에 따라 한 장이
                                                                     #   괘선 오차 6.0 px 로 통과해 칸이 어긋난 채 적재된다)
minedocscan template variant /tmp/var/site/templates/synth_usage_log --scan /tmp/var/scans/scan_2030-01-07.pdf --page 4 --name synth_usage_log_b
# 요약이 안내한 두 줄을 판 A 의 template.yaml 에: family: synth_usage_log , concurrent: true
minedocscan run --fresh
minedocscan report                         # 동시 판 synth_usage_log: 고른 쪽 synth_usage_log 16, synth_usage_log_b 15, 정합 실패 0 …
minedocscan template print-layer /tmp/var/site/templates/synth_usage_log_b   # 15장
minedocscan template print-layer /tmp/var/site/templates/synth_usage_log     # 16장 — A 로 적재된 쪽만으로
# 두 판의 template.yaml 에 print_image: print.png → minedocscan run --fresh
```

## 손봐야 할 때

- 분류가 다른 양식과 헷갈린다 (`low_margin`) → 서식이 같고 제목만 다른 양식은 한 템플릿으로 합치고 제목 필드로 구분한다.
- 정합은 되는데 괘선 오차가 크다 → 기준 이미지가 기울었거나 `grid` 가 실제 괘선과 어긋나 있다. `template init` 을 다시 돌려 좌표를 비교한다.
  몇 쪽만 크게 떨어져 있으면 같은 날 섞여 쓰이는 다른 판이다 (위 절차).
- 가로 양식에서 괘선이 덜 잡힌다 → 페이지 전체가 아니라 `--roi` 로 표 하나만 지정한다.
- `add-region` 의 괘선이 인쇄된 표보다 많다 → 인쇄 층을 쪽이 적게 만들어 잔상이 남았거나, 인쇄 층 없이 채워진 기준 이미지에서 잡았다.
  쪽이 쌓인 뒤 층을 다시 만들어 잡는다.
- 인쇄가 든 칸이 비어 있어도 늘 검수 대기다 → 그 양식에 인쇄 층(`print_image`)이 없다. role 표(`meter`·`shifts`·`tally`)의 칸만 인쇄 층으로 잰다.
- `template check` 가 "인쇄 층이 칸의 절반 넘게 덮은 표의 손으로 쓰는 칸"을 알린다 → 칸 자리가 틀렸거나(값 자리가 인쇄 위), 층에 잔상이 있거나,
  다른 양식·다른 판의 층이다. `template preview --print` 로 본다.
- 동시 판의 키가 다르다는 오류로 사이트 팩이 읽히지 않는다 → 한 판에만 표·열·행·필드를 더하거나 고쳤다. 다른 판도 똑같이 고친다.
