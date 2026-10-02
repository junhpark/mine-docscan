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

[crosscheck.haul]
# 교차검증에서 뺄 광종 (한쪽 양식에만 있는 행)
exclude_materials = ["SURFACE"]
```

코드에서는 `site.option("crosscheck.haul", "exclude_materials", [])` 처럼 읽는다. 새 옵션이 필요하면 여기에 절을 추가한다.

## template.yaml

```yaml
name: synth_haul_log               # 템플릿 이름 (영문 소문자·밑줄). DB 의 template_name
title: Dump truck daily haul log   # 사람이 읽는 이름
reference_image: reference.png
dpi: 200                           # 좌표계의 해상도. 설정의 dpi 와 같아야 한다
page_size: [2339, 1654]            # 기준 이미지의 (폭, 높이) px
handler: haul                      # generic | inspection | haul
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
  - {name: vehicle_no, kind: handwritten_text, bbox: [340, 290, 730, 355]}    # x0, y0, x1, y1
```

### 열의 종류 (`kind`)

| kind | 처리 |
|---|---|
| `printed` | 읽지 않는다. 행 메타에서 같은 이름의 값을 가져온다 (`rows[].<열 이름>`) |
| `handwritten_text` | 잉크가 있으면 인식 백엔드로 보낸다 |
| `handwritten_number` | 위와 같되 숫자로 해석한다. `haul` 핸들러에서는 글씨 덩어리 배정으로 값 유무를 정한다 |
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
- `role: log` 는 페이지마다 차량번호·작성자가 필요하다. 인식기가 생기기 전에는 라벨로 준다.

## labels/pages.json

인식기가 아직 읽지 못하는 페이지 메타를 사람이 붙여 둔 것이다. 인식기가 생기면 필요 없어진다.

```json
{
  "<파일명(확장자 제외)>":          {"date": "2030-01-07"},
  "<파일명(확장자 제외)>#<페이지>":  {"vehicle_no": "V-101", "operator": "ALPHA"}
}
```

우선순위: 페이지 라벨 > 문서 라벨 > 파일명 규칙.

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
   minedocscan info                         # 템플릿이 읽히는지, 셀 수가 맞는지
   minedocscan run <그 양식이 든 파일> --fresh
   minedocscan report                       # 정합 통과 수, 괘선 오차
   ```
   정합 이미지는 `WORK_ROOT/aligned/<문서 ID>/pNN_<템플릿>.png` 에 있다. 괘선이 템플릿 좌표와 겹치는지 눈으로 본다.
6. **기준 갱신** — `minedocscan regress --update` 로 새 양식을 포함한 수치를 기준으로 저장한다.

### 손봐야 할 때

- 분류가 다른 양식과 헷갈린다 (`low_margin`) → 서식이 같고 제목만 다른 양식은 한 템플릿으로 합치고 제목 필드로 구분한다.
- 정합은 되는데 괘선 오차가 크다 → 기준 이미지가 기울었거나 `grid` 가 실제 괘선과 어긋나 있다. `template init` 을 다시 돌려 좌표를 비교한다.
- 가로 양식에서 괘선이 덜 잡힌다 → 페이지 전체가 아니라 `--roi` 로 표 하나만 지정한다.
