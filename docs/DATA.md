# 데이터 관리

## 무엇이 어디에 있는가

| 무엇 | 어디 | 저장소에 넣는가 |
|---|---|---|
| 코드, 문서, 합성 데이터 생성기 | 이 저장소 | 예 |
| 스캔 원본 (PDF·이미지) | 공유 드라이브의 `mine-docscan/` (= `ARCHIVE_ROOT`) | **아니오** |
| 사이트 팩 (템플릿·기준 이미지·라벨·회귀 기준) | 공유 드라이브의 `mine-docscan/site-packs/<현장>/` (= `SITE`) | **아니오** |
| 정답 (CSV, 엑셀) | 공유 드라이브, 스캔 원본 옆 | **아니오** |
| 검수 기록 (`reviews/reviews.jsonl`) | 사이트 팩 안 (= `SITE/reviews/`) | **아니오** — 사람이 입력한 값, 다시 만들 수 없다 |
| 정합 이미지, SQLite DB, 리포트 | 각자의 로컬 디스크 (= `WORK_ROOT`) | 아니오 (언제든 다시 만든다) |
| 숫자 인식기 모델 (`models/<이름>/`) | 사이트 팩 안 (= `SITE/models/`) — 현장 글씨로 학습한 것 | **아니오** (예외: 합성 셀만으로 만든 `tests/fixtures/digits-fixture`) |
| 메타 필드 모델 (`models/<이름>/`, `classes.json` 에 이름·차량번호) | 사이트 팩 안 (= `SITE/models/`) | **아니오** (예외: 합성 값만으로 만든 `tests/fixtures/meta-digits`, `meta-operator`) |
| 학습용 크롭(`export-crops`), 틀린 칸 모아 보기(`recognizer eval --errors`) | 저장소 밖 (기본 `WORK_ROOT/recognizer-errors`) | **아니오** — 현장 글씨. git 작업 트리 안이면 도구가 거절한다 |
| 그 밖의 모델 가중치 | 로컬 또는 모델 저장소 | 아니오 |

공유 드라이브의 현재 배치:

```
mine-docscan/                 ← MINEDOCSCAN_ARCHIVE_ROOT
  DB_scans/                   하루치 묶음 PDF (파일명 YY.MM.DD.pdf)
  DB_excel/                   현장에서 입력한 작업일보 엑셀 (정답으로 쓸 수 있는 자료)
  DB_jumbo/                   천공 작업 내역 엑셀
  site-packs/<현장>/          ← MINEDOCSCAN_SITE
```

### 새 컴퓨터에서 시작하기

1. 저장소를 받고 `pip install -e ".[dev]"`, `pytest` 로 합성 시험이 통과하는지 본다. 여기까지는 데이터가 필요 없다.
2. 공유 드라이브를 그 컴퓨터에 동기화하거나(데스크톱 동기화 앱) 필요한 폴더를 내려받는다.
3. 환경변수 또는 `minedocscan.toml` 로 경로를 지정한다.
   ```bash
   export MINEDOCSCAN_ARCHIVE_ROOT="…/mine-docscan"
   export MINEDOCSCAN_SITE="…/mine-docscan/site-packs/<현장>"
   export MINEDOCSCAN_WORK_ROOT="$HOME/minedocscan-work"       # 동기화 폴더 밖, 로컬 디스크
   ```
4. `minedocscan info` 로 확인하고 `minedocscan regress` 로 기준 수치가 재현되는지 본다.

`WORK_ROOT` 를 동기화 폴더 안에 두지 않는다. 정합 이미지가 수천 장 생기고 SQLite 파일이 동기화 중에 깨질 수 있다.

## 스캐너와 스캔 설정

보유한 스캐너는 Canon imageFORMULA R10 이다. 지금 가진 스캔이 이 기기로 만든 것인지는 확인하지 못했다(원본은 300 dpi 다).

| 사양 (제조사 공개) | 값 | 파이프라인에서의 의미 |
|---|---|---|
| 형태 | USB 급지식 휴대용, 양면 | 급지식은 기울기와 미세한 배율 차이가 생긴다 → 정합이 흡수한다 |
| 급지대 | 20매 | 하루치 묶음(25–30쪽)은 나눠 넣게 된다. 파일이 나뉘어도 파일명에 날짜가 있으면 된다 |
| 출력 해상도 | 150 / 200 / 300 / 400 / 600 dpi | 300 dpi 로 고정한다 |
| 색상 | 컬러 / 회색조 / 흑백 | 흑백(2값)은 쓰지 않는다. 잉크·체크 판정이 회색조를 전제로 한다 |
| 소프트웨어 | 본체 내장 CaptureOnTouch Lite | 저장 폴더가 곧 접수 폴더가 된다 (폴더 감시, ROADMAP M5) |

권장 설정 — 아직 이 기기로 시험하지 않았다. 설정을 정하면 그 설정으로 스캔한 표본으로 회귀 기준을 다시 잡는다.

- 해상도 300 dpi 고정. 자동 해상도는 끈다.
- 회색조(또는 컬러) 하나로 고정한다.
- 문자 강조, 배경 제거·매끄럽게, 컬러 드롭아웃, 가장자리 강조 같은 **화질 보정은 끈다.** 켜면 잉크 판정의 기준이 달라진다.
- 기울기 보정은 켜도 된다.
- 자동 회전(문자 방향 인식)은 끄고 넣는 방향을 일정하게 한다. 90°·180° 돌아간 쪽의 정합은 아직 시험하지 않았다.
- 양식이 단면이면 단면으로 스캔한다. "빈 쪽 건너뛰기"는 흐리게 쓴 쪽을 빈 쪽으로 볼 수 있다.
- 형식은 PDF, 파일명에 날짜(지금 규칙은 `YY.MM.DD`).

## 커밋하면 안 되는 것

현장 문서에는 작업자 이름, 서명, 차량번호가 있다. 저장소에 한 번 들어가면 나중에 지워도 기록에 남는다.

- 스캔 원본과 그 일부를 잘라낸 이미지 — 검수 화면의 갈무리도 마찬가지다
- 기준 이미지(`reference.png`)와 실제 템플릿 YAML — 행렬 양식의 머리글에 이름·차량번호가 인쇄되어 있다
- 페이지 라벨, 정답 CSV·엑셀, 검수 기록(`reviews.jsonl` — 적힌 값과 출처가 들어 있다)
- 학습한 모델(`models/`), 내보낸 크롭, 틀린 칸 모아 보기 — 현장의 글씨에서 나온 것. 문서·PR·이슈에도 붙이지 않는다 (수치만 옮긴다).
  메타 필드 모델의 `classes.json` 은 이름·차량번호의 목록 그 자체다
- 실제 이름·차량번호를 예시로 쓴 문서·주석·테스트·커밋 메시지
- API 키와 비밀값 (`.env`, `minedocscan.toml`)

**이름·차량번호가 들어 있는 파일** (전부 저장소 밖):

| 파일 | 위치 | 무엇이 |
|---|---|---|
| 템플릿 YAML, 기준 이미지 | `SITE/templates/<양식>/` | 행렬 머리글의 인쇄된 이름·차량번호(`header_operator`, `header_vehicle_no`) |
| 페이지 라벨 | `SITE/labels/pages.json` | 쪽마다 차량번호·작성자 |
| 검수 기록 | `SITE/reviews/reviews.jsonl` | 입력한 차량번호·작성자 (`page-fields`, `meta-check`) |
| 메타 필드 모델의 종류 목록 | `SITE/models/<이름>/classes.json` | 고를 수 있는 이름·차량번호 (카드·학습 로그에는 없다 — 종류의 수와 분포만) |
| 내보낸 메타 크롭 | `OUT/<split>/meta/<키>/*.png`, `OUT/<split>/meta/labels.jsonl` | 글씨 그림과 그 값 |
| 틀린 칸 모아 보기 | `WORK_ROOT/recognizer-errors/` (또는 `--errors` 의 경로) | 글씨 그림과 기계·정답 값 |
| DB | `WORK_ROOT/minedocscan.db` | `doc_page_meta`, `prod_haul`, `eq_assignment_obs` 의 값 |

`pages --meta-mismatch`, `eval --meta`, `report`, `info`, `recognizer list`·`eval` 의 출력에는 값을 찍지 않는다 (수만). 값은 검수 화면(127.0.0.1)에서만 본다.

`.gitignore` 가 이미지·PDF·엑셀·CSV·DB·`sites/`·`work/` 를 기본으로 막는다(`tests/fixtures/` 만 예외).
테스트에 이미지가 필요하면 `tools/synth.py` 로 만든다. 문서의 예시는 합성 데이터의 값(`T01`, `V-101`, `ALPHA`)을 쓴다.

## 시험 데이터 두 가지

### 합성 데이터 — 저장소만 있으면 된다

`minedocscan synth <폴더>` 또는 테스트의 `synth` 픽스처. 가상의 양식 세 종에 무엇을 적었는지 알고 있으므로
분류·정합·체크 판정·값 유무·교차검증·배차 관측을 정답과 정확히 비교할 수 있다.

```
<폴더>/site/           합성 사이트 팩
<폴더>/scans/          하루에 PDF 한 개 (점검표 → 차량별 일보 → 행렬 [→ 가동 일보])
<폴더>/truth.json      날짜별 정답과 기대 수치 (--usage-logs 면 "usage": 쪽마다 eq_usage_daily 의 정답)
<폴더>/answers.json    오라클 백엔드·eval 용 정답
```

`--usage-logs` 는 날마다 묶음 끝에 장비 가동 일보 두 종(세로: 작업 표 + 계기, 가로: 작업량 표 + 근무 시각 + 계기)을 붙인다
(`--usage-only` 면 가동 일보만). 계기가 이어지는 장비, 하루 두 장, 며칠 빠진 장비, 시작을 잘못 적은 날, 총 ≠ 종료 − 시작, 계기 대신 시각
(점으로 쓴 08.00 포함), 계기가 빈 장비, 대응표에 없는 장비명, 작업량 표 위의 메모가 들어 있다. 기본 합성 데이터는 그대로다.

한계: 글자가 영문 내장 글꼴이거나 자체 획(`tools/handfont.py` — 숫자 칸)이다. 이 데이터로 **한글 손글씨 인식률을 말할 수 없다.** 잴 수 있는 것은 기하와 논리다.
나중에 양식을 다양하게 늘리는 작업(유류일지, 환경일지 등의 가상 양식)도 이 생성기를 확장하는 방식으로 한다.

### 실데이터 — 정확도는 여기서만 말한다

- **회귀 기준**: 사이트 팩의 `expected/regression.json`. 입력 목록과 그때의 리포트 수치다.
  ```bash
  minedocscan regress                               # 기준과 비교. 다르면 항목별로 보여 주고 종료 코드 1
  minedocscan regress --update                      # 지금 결과를 새 기준으로
  minedocscan regress --update --inputs A B         # 처음 만들 때: archive_root 기준 상대경로
  pytest -m realdata                                # 같은 검사
  ```
  수치가 달라지면 좋아진 것인지 망가진 것인지 사람이 확인한다. 기준을 갱신할 때는 무엇이 왜 바뀌었는지 커밋 메시지에 적는다.
  회귀는 **검수 파일을 읽지 않는다** — 코드(기계)의 수치만 본다. 검수가 쌓여도 기준과 어긋나지 않는다.
  기준은 실행 환경(OS, OpenCV 버전)에 따라 체크 판정 몇 건이 달라질 수 있다. 다른 컴퓨터에서 처음 돌렸을 때 차이가 나면
  코드가 아니라 환경 차이일 수 있으므로 먼저 변경 없는 코드로 비교한다.
- **정답과 비교**:
  ```bash
  minedocscan eval --inspection-csv <정답 CSV 폴더>        # 점검표: 파일명 YYMMDD.csv
  minedocscan eval --answers <answers.json>                # 일반 형식
  ```
- **오라클 확인**: `minedocscan run --inspection-csv <폴더> <입력>` 뒤 `eval` → CER 0 이어야 한다.

### 전체 묶음(약 150일치)을 돌리기

```bash
minedocscan run DB_scans --fresh                 # 처음 한 번. 진행 표시: [12/150] 파일 · 28쪽 · 지난 시간 · 남은 시간
minedocscan run DB_scans --skip-existing         # 그 뒤로: 끝까지 처리된 파일은 건너뛰고 failed 만 다시 한다
minedocscan report --by-month                    # 양식 × 월: 쪽 수, 정합 통과, 괘선 오차 — 어느 달에 양식이 바뀌었나
minedocscan pages --status unknown_form --thumbs # 양식을 못 찾은 쪽 목록 + 1/4 미리보기 (WORK_ROOT/thumbs/, 저장소 밖)
minedocscan pages --status error                 # 쪽 하나에서 예외가 난 것 (예외 종류와 메시지)
```

- 깨진 파일이 있어도 끝까지 간다. 그 문서는 `failed`, 요약에 목록이 나오고 종료 코드는 1 이다. 반쯤 쓰인 행은 남지 않는다.
- PDF 라이브러리가 **복구해서 연** 파일(끝이 잘린 파일 등)은 기본으로 `failed` 다 (`[pipeline] damaged_pdf = "fail"`).
  어느 스캐너의 멀쩡한 파일이 늘 "복구가 필요했습니다"로 실패하면 `damaged_pdf = "warn"` (또는 `MINEDOCSCAN_DAMAGED_PDF=warn`) —
  그 문서는 처리하고 `doc_document.warning` 과 요약·`report` 에 경고로 남긴다 (종료 코드는 그대로 0).
- **`--skip-existing` 을 쓰면 안 되는 때**: 템플릿(사이트 팩)이나 인식기·판정 규칙을 바꾼 뒤. 그때는 `--fresh` 로 처음부터 다시 돌린다.
  건너뛰기는 파일 해시만 보고 결과가 유효한지는 모른다.
- 다른 컴퓨터에서 같은 DB 를 쓰려면 `archive_root` 만 그 컴퓨터의 경로로 준다. 원본은 `source_rel`(archive_root 기준 상대경로)로 찾는다.

### 검수값으로 평가하기

운반 횟수의 정답은 검수 도구로 만든다 (ADR 0007). 검수 기록은 사이트 팩의 `reviews/reviews.jsonl` 에 쌓이고
(한 줄 = 한 건, 추가 전용), DB 를 지우고 다시 돌려도 파이프라인이 시작할 때 읽어 들여 그대로 붙는다.

```bash
minedocscan review serve --queue page-fields --reviewer jp              # 일보의 차량번호·작성자 (수동 라벨 대신). 날짜순
minedocscan review serve --queue haul-numbers --n 1500 --reviewer jp   # 표본 1,500셀(빈 칸 10 %), 브라우저 127.0.0.1:8765
minedocscan review serve --queue mismatch --reviewer jp                 # 교차검증 불일치 칸: 두 문서의 셀을 같이 본다
minedocscan review serve --queue pending --template <양식> --reviewer jp  # 운영용: 기계 값을 미리 채워 준다
minedocscan review stats                                                # 판정·양식·날짜·검수자별 건수
minedocscan review export-answers answers-test.json --split test        # test 날짜의 value/empty 검수 → 정답 파일 (illegible 제외)
minedocscan run DB_scans --fresh                                        # 새 인식기로 다시 돌린다. 검수값은 그대로 붙는다
minedocscan eval --answers answers-test.json --target raw --only-listed --split test   # 기계가 읽은 값을 검수값과 비교
minedocscan report                                                      # 횟수 일치율: 최종 값 기준 / 기계 값 기준
minedocscan review export-crops ~/minedocscan-crops --split train       # 학습용 셀 이미지(원본 해상도) + labels.jsonl
```

- **평가셋은 날짜로 나눈다** (ADR 0009). `site.toml` 의 `[eval] split_salt`, `test_share` 가 정하고, 검수가 늘어도 분할은 바뀌지 않는다.
  `test` 는 학습·문구 사전·임계값 조정에 쓰지 않는다. `review stats` 가 분할별 셀 수와 날짜 수를 보여 준다.
- 내보낸 크롭은 `OUT/<split>/<kind>/<이름>.png` 와 `OUT/<split>/labels.jsonl` 이다 (이름은 field_id 의 `:` 처럼 파일 이름에 못 쓰는 글자를
  바꾼 것 — 읽는 쪽은 `labels.jsonl` 의 `file` 을 쓴다. 라벨에는 규격 `spec` 과 잉크 판정 `inked` 가 같이 적힌다). 기본은 원본 300 dpi 를 호모그래피로 다시 정합한
  템플릿 좌표 1.5배 크기이고(`--res aligned` 면 200 dpi 정합 이미지), 라벨에 어느 해상도인지 적힌다. **저장소 밖에만** 둔다 — git 작업 트리 안이면 거절한다.

- **검수 규칙 (숫자 칸)** — 이 규칙이 숫자 인식기의 "빈 칸"과 "거절"의 정답이 된다 (tasks/0003 4.3):
  - 칸에 이 칸의 숫자가 적혀 있으면 **적힌 그대로** 입력한다 (두 문서가 달라도 각각 적힌 대로).
  - **X 표, 덧칠해 지운 칸, 이웃 칸에서 넘어온 글씨, 칸 위에 걸쳐 쓴 메모**는 이 칸의 값이 아니다 → 비워 두고 Enter (**빈 칸**).
  - 숫자인 것 같지만 읽을 수 없으면 `?` 후 Enter (**읽을 수 없음**). 인식기는 이것을 "거절"로 배운다.
- `--target raw` 를 써야 한다. `final` 은 검수값 자신이라 언제나 맞는다.
- `--only-listed`: 표본만 검수했으므로 정답에 있는 셀만 평가한다. 없으면 그 표의 나머지 셀을 빈 칸 정답으로 친다.
- 결과에는 정답이 빈 칸인 셀과 값이 있는 셀의 정확도가 따로 나오고, 값 유무 판단의 정밀도·재현율이 같이 나온다.
- 검수 파일은 사람이 입력한 유일한 데이터다. 사이트 팩과 함께 백업한다. 한 번에 한 사람만 입력한다.
- 검수자가 한 사람이면 그 사람의 오독이 정답에 들어간다. 표본의 일부는 두 번째 사람이 따로 입력해 일치도를 본다 (아직 도구에 없다).

### 숫자 인식기: 학습에서 평가까지

운반 횟수 숫자 칸의 인식기(`digits`, ADR 0011·0012). 학습만 torch 가 필요하다 (`pip install -e ".[train]"`). 현장 PC 의 추론은 OpenCV 만 쓴다.

```bash
minedocscan review export-crops ~/crops --split train --kind handwritten_number --include-illegible
                                            # 검수한 숫자 칸 (illegible 은 "거절"로 학습). test 는 내보내지 않는다
minedocscan recognizer train --crops ~/crops --name digits-v1
                                            # → <site>/models/digits-v1/ (model.onnx, card.json, train-log.jsonl). CPU 몇 분
minedocscan recognizer eval --crops ~/crops --model digits-v1 --split val --errors
                                            # 검증 날짜에서: 정확도, 값별 표, 많이 틀린 쌍, 임계값별 자동 적재율·오류율, 틀린 칸 모아 보기
minedocscan recognizer list                 # 사이트 팩의 모델과 카드 요약
# 설정: [recognize.by_kind] handwritten_number = "digits",  [recognize.digits] model = "digits-v1"
minedocscan info                            # 고른 백엔드와 모델(규격, 자동 적재 기준, 학습 셀 수)
minedocscan run DB_scans --fresh
minedocscan review export-answers answers-test.json --split test
minedocscan eval --answers answers-test.json --target raw --only-listed --split test   # 자동 적재 오류율(분자·분모)이 같이 나온다
minedocscan report                          # 백엔드별 칸 수·자동·대기, 횟수 일치율(기계 값 기준)
```

- **test 는 마지막에 한 번 본다.** 학습 명령은 `labels.jsonl` 에 test 줄이 하나라도 있으면 거절한다. 학습을 언제 멈출지, 온도, 자동 적재 기준,
  규격·구조의 비교는 전부 검증 날짜(train 날짜 안에서 날짜 단위로 뗀 20 %)로 한다. test 를 본 뒤에 모델을 다시 고르지 않는다.
- 자동 적재 기준은 검증 날짜에서 자동 적재 오류율이 목표(`--target-auto-error`, 기본 1 %) 이하인 가장 낮은 임계값이다. 카드에 임계값별 표가 있다.
  표를 보고 더 보수적으로 하려면 설정의 `[recognize.digits] auto_accept_conf`. 검증 날짜의 실제 셀이 하나도 없으면(날짜가 적을 때) 기준을 정하지 않고
  (자동 적재 없음) 그렇게 알린다 — 날짜가 늘면 다시 학습한다. 검증·평가는 잉크 판정이 "값 있음"이던 칸만 센다 (인식기가 실제로 받는 칸).
- **검증 칸이 적으면 기준이 없다.** 그 임계값에서 자동 적재된 검증 칸이 100개(`--min-val-auto`) 미만이면 오류 0 이어도 기준을 정하지 않는다.
  기준이 나오면 그 옆의 **95 % 상한**을 본다(학습 출력, `info`, `recognizer list`) — 오류 0 이어도 100칸이면 상한 3.7 %, 1 % 를 뒷받침하려면
  약 380칸이다. 목표·검증 비율·정답 분량은 과제 책임자가 정한다 (ADR 0012 "정할 것").
- 크롭의 규격(해상도·배율·여유)은 학습한 모델이 기억한다. 다른 규격을 비교하려면 `export-crops --res aligned --scale 1` 처럼 따로 내보내 따로 학습한다
  (한 폴더에 규격이 섞이면 거절한다). 정답이 몇 백 셀뿐이어도 한 번 돌려 볼 수 있다 — 합성 셀(`tools/synth_cells.py`)이 반을 채운다. 수치가 거칠다는 것만 안다.
- 범위: `site.toml` 의 `[haul] trips_max` 보다 큰 값은 신뢰도가 높아도 검수로 간다.
- 인식기 수치는 실데이터 검증·test 날짜로만 말한다. 합성 셀의 수치는 학습·추론 경로가 맞는지의 확인이다.

### 쪽 메타(차량번호·작성자): 정답에서 감사까지

일보의 차량번호(네 자리)·작성자를 기계가 채우게 하는 순서다 (tasks/0004, ADR 0013·0014). 학습만 torch 가 필요하다.

```bash
minedocscan review serve --queue page-fields --reviewer jp     # 1. 날짜순으로 20일치쯤(약 200쪽). 기계 값은 보이지 않는다
minedocscan review stats                                       #    분할별 쪽 수 — test 날짜의 쪽은 평가에만 쓰인다
minedocscan review export-crops ~/meta --meta --split train    # 2. OUT/train/meta/<키>/ + OUT/train/meta/labels.jsonl (사람·파일명 값만)
minedocscan recognizer train --crops ~/meta --meta-key vehicle_no --name veh-v1 --cv 5    # 숫자: 읽고 목록에서 고른다
minedocscan recognizer train --crops ~/meta --meta-key operator   --name op-v1  --cv 5    # 이름: 분류기
minedocscan recognizer eval  --crops ~/meta --model op-v1 --split val --errors            # 묶음 교차 읽기에서 틀린 쪽 (저장소 밖)
minedocscan recognizer list                                    # 기준·상한은 묶음 교차 읽기 전체에서 (카드)
# 설정: [recognize.meta] vehicle_no = "veh-v1", operator = "op-v1"   → minedocscan info 로 기준(상한) 확인
minedocscan run DB_scans --fresh                               # 3. 기계가 빈 쪽을 채운다. 라벨·검수가 있는 쪽은 대조만
minedocscan report                                             #    키마다 출처별 쪽 수, 일보의 자리가 어느 출처로 정해졌나
minedocscan eval --meta --split test                           #    정확도·자동 적재 오류율·배차가 바뀐 쪽·자리
minedocscan review serve --queue page-fields --audit 100 --reviewer jp   # 4. 표본 감사: 자동 적재된 쪽도 기계 값 없이 다시 본다
minedocscan eval --meta
minedocscan review serve --queue meta-check --reviewer jp      # 5. 기계와 라벨·검수가 다른 쪽. 종이를 보고 정한다
minedocscan pages --meta-mismatch --meta-key date.day          # (월·일 필드를 넣었으면) 다른 날의 쪽이 섞인 묶음
```

- **정답은 기계 값을 보지 않고 만든다.** `page-fields` 는 기계 값을 보여 주지 않고, 기계가 채운 키는 묻지 않는다. 그래서 모델을 쓰기 시작하면
  자동 적재된 쪽의 오류를 잴 정답이 생기지 않는다 — `--audit N` 이 날짜별로 고르게 뽑은 쪽(씨앗으로 고정)을 기계의 상태와 상관없이 다시 보여 준다.
  기계가 채운 값은 정답이 아니다 (`eval --meta` 는 검수·라벨·파일명 값만 정답으로 센다).
- **정답이 적다.** 쪽마다 한 칸이라 20일치가 약 200쪽이고, 검증으로 20 % 를 떼면 기준(자동 적재된 검증 읽기 100개)이 나오지 않는다.
  `--cv 5` 는 train 날짜를 다섯 묶음으로 나눠 묶음마다 "나머지로 학습 → 그 묶음 읽기"를 하고, 모은 읽기 전체로 온도·기준을 정한다 (ADR 0014).
  내보내는 모델은 train 날짜 전부로 학습한 것이다. 목표 오류율 기본 2 % (`--target-auto-error`).
  묶음마다의 모델은 남기지 않으므로 `--cv` 모델의 `recognizer eval --split val` 은 학습 때 묶음 교차로 읽은 결과(모델 폴더의 `cv-reads.jsonl`,
  값 없이)로 표를 내고, `--errors` 는 그 필드의 크롭을 `--crops` 폴더에서 찾아 그린다.
- **목록은 학습 때 정해진다.** 새 차·새 사람은 다시 학습하기 전에는 "목록에 없는 값"이나 기준 미만으로 남아 `page-fields` 로 온다 — 그 쪽은
  검수로 채우고, 쌓이면 다시 학습한다. 분류기는 학습 날짜에 예가 3개 미만인 사람을 종류로 두지 않는다.
- **차량번호는 글씨체가 아니라 숫자로 읽는다.** 학습 데이터에서는 번호마다 쓰는 사람이 거의 정해져 있어서, 글씨체로 번호를 외운 모델도
  검증 수치가 좋다. `eval --meta` 의 "배차가 바뀐 쪽"(그 작성자의 평소 차가 아닌 쪽)의 정확도를 따로 본다.
- 라벨이 틀렸으면 기계 값과 `mismatch` 로 드러난다 (`report`, `pages --meta-mismatch`). `meta-check` 화면에서 종이를 보고 입력한다 — 입력한 값이
  검수가 되어 라벨을 이긴다. 라벨 파일도 고친다.

### 장비 가동 일보: 템플릿에서 계기 검산까지

가동 일보(중기운행일보·점보·로우더 작업일보)의 템플릿을 만드는 순서와 입력 (tasks/0005, ADR 0015·0016).

```bash
minedocscan template init <빈 양식 PDF> --name <이름> --roi …    # 뼈대 → 표마다 role, 칸마다 format, 장비명·운전자 필드에 meta_key
minedocscan template check   <site>/templates/<이름>            # 오류를 전부 (역할에 필요한 칸, 형식과 종류, 겹치는 칸, 쪽 밖 …)
minedocscan template preview <site>/templates/<이름> --scan <PDF> --page N   # 칸이 실제 글씨에 맞는지 (WORK_ROOT/template-preview)
# site.toml [equipment.aliases]: 일보에 적는 이름 → 점검표의 장비 키 (모르는 것은 비워 둔다)
minedocscan run DB_scans --fresh                                # 스키마 6
minedocscan review serve --queue page-fields --reviewer jp      # 장비명·운전자 (후보 = 대응표의 이름 + 라벨·검수에 나온 값)
minedocscan review serve --queue readings --reviewer jp         # 계기 칸: 쪽마다 시작·종료·총을 한 번에
minedocscan report                                              # 가동 기록: 계기 칸의 종류별, 가동 시간의 근거별, 검산(이어짐 / 어긋남 …)
minedocscan review serve --queue usage-check --reviewer jp      # 계기가 이어지지 않는 곳: 어제의 종료 칸과 오늘의 시작 칸을 같이
minedocscan review stats                                        # 형식별·대기열별
```

- **계기 칸의 규칙**: 계기 값은 숫자 그대로(1234.5), 시각은 콜론으로(08:00) — 점으로 쓴 시각(08.00)도 콜론으로 입력. 숫자인지 시각인지는 사람이 정한다
  (코드는 콜론만 본다). 빈 칸은 비워 두고 `Enter`. 형식에 맞지 않는 값(12:75, 1234,5)은 화면과 서버가 거절하고 검수 파일에 남지 않는다.
- `readings` 는 **기계 값도 앞날의 값도 보여 주지 않는다** — 보여 주면 그 값을 따라 적는다. 어제와 오늘을 같이 보는 것은 `usage-check` 에서만.
  `--audit N` 은 잉크와 상관없이 날짜별로 고르게 N 쪽 — 빈 칸으로 넘어간 계기 칸을 잴 정답.
- `usage-check`: 종이와 다르게 입력된 칸만 고치고 `Enter` — 검산이 맞게 되면 빠진다. 한 칸을 고쳐도 여전히 어긋나면 남는다.
  아무것도 고치지 않고 `Enter` 하면 "종이에 적힌 대로"를 확인한 것이고 끝난다 — 어긋남은 `xcheck_usage` 와 리포트에 그대로 남는다
  (값을 맞춰 넣지 않는다, ADR 0006). 빠진 날이 있는 장비의 `gap` 은 확인하고 넘어가는 것이 맞다.
- 장비명을 고치면 예전 장비와 새 장비 양쪽의 계기 검산이 바로 다시 계산된다.
- **돌려줄 수치**(이름·번호 없이): 양식별 쪽 수와 정합 통과율, 계기 칸이 있는 쪽 / 시각 / 빈 쪽, 장비 수, 연속성 검산의 결과별 수와 어긋난 것의 원인
  (빠진 날 / 잘못 적음 / 다른 장비), 한 시간에 입력한 쪽 수, 작업량 표에 값이 있는 칸의 비율 (tasks/0005 8절).

### ✓ 판정의 정답

점검표의 이상 유/무 체크(✓)가 맞게 판정되었는지 잴 정답이다 (tasks/0004 단계 6).

```bash
minedocscan review serve --queue checks --n 300 --reviewer jp  # 장비 행 300개 (날짜별로 고르게, --seed 로 고정)
minedocscan eval --checks [--split test]                       # 기계의 답 × 정답 표, 정확도(구간), 판정 불가, column_unused
minedocscan review stats                                       # ✓ 검수 행 수
```

- 항목 = 장비 행 하나. 행 띠(테두리 없음)와 유·무 두 칸을 크게 보여 준다. **기계의 판정은 보여 주지 않는다** — 판정 불가였던 행, 점검을 하지 않은
  날(`column_unused`)의 행도 표본에 들어 있다. 표시가 없다는 것도 정답이다.
- 키: `1` 유, `2` 무, `Enter` 표시 없음, `?` 모름 — 누르면 저장하고 다음 행. `PgUp`/`PgDn` 으로 돌아가면 전에 고른 답이 보이고, 다시 고르면 고쳐 저장한다.
- ✓ 가 경계선을 넘어 오른쪽 칸까지 그려졌으면 **시작한 칸**이 표시한 칸이다 (CLAUDE.md "실데이터에서 배운 것"). 두 칸 다 표시했거나 무엇인지 모르겠으면 `?`.
- 저장은 새 판정 종류가 아니라 두 칸의 검수 두 건이다: 유 = 유 칸 `value`·무 칸 `empty`, 무 = 반대, 표시 없음 = 둘 다 `empty`, 모름 = 둘 다
  `illegible`. 그래서 `insp_daily.abnormal` 이 바로 그 답을 따르고, 기계의 판정(`has_value_raw`)은 그대로 남아 `eval --checks` 가 비교한다.

### 정답 형식

`answers.json` — 양식에 상관없는 일반 형식.

```json
[
  {"work_date": "2030-01-07", "template": "synth_inspection", "region": "main",
   "field_name": "remark", "row_key": "EQ-0201", "text": "oil leak"},
  {"source": "scan_2030-01-07#2", "template": "synth_haul_log", "region": "haul",
   "field_name": "trips_day", "row_key": "ORE|L0", "text": "7"}
]
```

`source`(`<파일명>#<페이지>`)는 같은 날 같은 양식이 여러 장일 때, `work_date` 는 한 장뿐일 때 쓴다.
어떤 표에 정답이 하나라도 있으면 그 표의 수기 셀 전부를 평가하고, 정답에 없는 셀은 빈 칸이 정답이다.
형식이 있는 칸(`format`)의 `text` 는 정규화한 표기다 (`1234.5`, `08:00`, `08:00~12:00`). `export-answers` 와 `eval` 은 비교할 때 다시 정규화하므로
`8:00` 과 `08:00` 은 같은 값이다. 기본 형식이 아닌 칸은 `eval` 에서 `<양식>/<종류>/<형식>` 으로 따로 묶인다.

점검표 CSV — 기존 연구팀 형식. 파일명 `YYMMDD.csv`, cp949, 머리 3줄 뒤에 양식의 행 순서대로 `구분,형식,등록번호,점검내역`.

## 평가셋을 키울 때

- 스캔과 정답의 **기간이 겹쳐야** 한다. 지금 가진 스캔 묶음과 현장 입력 엑셀은 기간이 달라서 엑셀을 정답으로 쓰지 못하고,
  같은 기간의 엑셀은 구할 수 없다. 숫자 정답은 검수 도구로 직접 만든다 (ADR 0007).
- 사람이 검수 도구에서 입력하거나 고친 값(`reviewed`)이 그대로 정답이 된다. 따로 라벨링 작업을 만들지 않는다.
- 스캔 원본은 300 dpi 다. 파이프라인은 200 dpi 로 렌더링해서 쓰므로, 인식기 학습용 크롭을 내보낼 때는 어느 해상도인지 같이 기록한다.
- 인식 백엔드를 비교할 때는 같은 평가셋, 같은 지표, 같은 전처리(정합된 셀 크롭)로 비교한다.
