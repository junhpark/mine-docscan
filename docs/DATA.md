# 데이터 관리

## 무엇이 어디에 있는가

| 무엇 | 어디 | 저장소에 넣는가 |
|---|---|---|
| 코드, 문서, 합성 데이터 생성기 | 이 저장소 | 예 |
| 스캔 원본 (PDF·이미지) | 공유 드라이브의 `mine-docscan/` (= `ARCHIVE_ROOT`) | **아니오** |
| 사이트 팩 (템플릿·기준 이미지·라벨·회귀 기준) | 공유 드라이브의 `mine-docscan/site-packs/<현장>/` (= `SITE`) | **아니오** |
| 정답 (CSV, 엑셀) | 공유 드라이브, 스캔 원본 옆 | **아니오** |
| 검수 기록 (`reviews/reviews.jsonl`) | 사이트 팩 안 (= `SITE/reviews/`) | **아니오** — 사람이 입력한 값, 다시 만들 수 없다 |
| 결정 기록 (`reviews/decisions.jsonl` — 문서·쪽의 날짜·버리기·되살리기·다른 종이, ADR 0019) | 검수 기록 옆 (`MINEDOCSCAN_REVIEWS` 를 주면 그 옆) | **아니오** — 값·이름은 없지만 현장의 운영 기록이다. 검수 기록과 같이 백업한다 |
| 접수 폴더 (스캐너 프로그램의 저장 폴더, `[paths] inbox`) | 그 PC 의 폴더 — 접수한 파일은 보관 폴더로 옮겨진다 | **아니오** |
| 정합 이미지, SQLite DB, 리포트 | 각자의 로컬 디스크 (= `WORK_ROOT`) | 아니오 (언제든 다시 만든다) |
| 숫자 인식기 모델 (`models/<이름>/`) | 사이트 팩 안 (= `SITE/models/`) — 현장 글씨로 학습한 것 | **아니오** (예외: 합성 셀만으로 만든 `tests/fixtures/digits-fixture`) |
| 메타 필드 모델 (`models/<이름>/`, `classes.json` 에 이름·차량번호) | 사이트 팩 안 (= `SITE/models/`) | **아니오** (예외: 합성 값만으로 만든 `tests/fixtures/meta-digits`, `meta-operator`) |
| 학습용 크롭(`export-crops`), 틀린 칸 모아 보기(`recognizer eval --errors`) | 저장소 밖 (기본 `WORK_ROOT/recognizer-errors`) | **아니오** — 현장 글씨. git 작업 트리·접수 폴더·보관 폴더 안이면 도구가 거절한다 |
| 인쇄 층(`print.png` — `template print-layer`), 다른 판의 기준 이미지(`template variant` 가 쓴 `reference.png`) | 사이트 팩 안 (= `SITE/templates/<양식>/`) | **아니오** — 현장 스캔에서 나온 것. 인쇄 층에는 늘 같은 자리에 쓰는 손글씨(이름·서명)의 잔상이 남는다. git 작업 트리 안이면 도구가 거절한다 (`print-layer` 는 합성 사이트 팩이면 `--allow-in-repo`) |
| 템플릿 미리보기(`template preview`, `--print`), 쪽 미리보기(`pages --thumbs`) | `WORK_ROOT/template-preview`, `WORK_ROOT/thumbs` | **아니오** — 실제 양식과 글씨. `template preview` 는 저장소 안이면 거절한다 |
| 엑셀 폴더(일별·월별 파일)와 그 기록 파일(`.minedocscan-export.json`), 홈에서 내려받은 엑셀 | `[export] excel_dir` (또는 `MINEDOCSCAN_EXCEL_DIR`) — 현장이 여는 폴더. 내려받은 것은 브라우저의 내려받기 폴더 | **아니오** — 이름·차량번호·값·원래 파일명이 들어 있다. DB 의 사본이라 지워도 다시 만든다. 저장소 안·접수 폴더 안·보관 폴더 안이면 도구가 거절한다. 기록 파일(`.json`)은 `.gitignore` 가 막지 않는다 |
| 가린 쪽 그림(`export masked-pages`) | 명령에 준 폴더 (저장소 밖) | **아니오** — 템플릿이 아는 자리만 가렸다. 가렸다고 커밋해도 되는 것이 아니다. git 작업 트리·접수 폴더·보관 폴더 안이면 도구가 거절한다 |
| 통합 DB(PostgreSQL)의 URL·비밀번호 (`MINEDOCSCAN_PUBLISH_URL`) | 그 컴퓨터의 환경변수만 — `minedocscan.toml` 의 `[publish]` 에 `url`·`dsn`·`password` 를 적으면 설정 오류. 실은 표는 현장 서버의 `[publish] schema` | **아니오** — 문서·이슈·로그에도 쓰지 않는다 (예시는 `postgresql://사용자:비밀번호@호스트/DB`) |
| 그 밖의 모델 가중치 | 로컬 또는 모델 저장소 | 아니오 |

공유 드라이브의 현재 배치:

```
mine-docscan/                 ← MINEDOCSCAN_ARCHIVE_ROOT (읽기 전용 — 접수만 intake/ 에 쓴다)
  DB_scans/                   하루치 묶음 PDF (파일명 YY.MM.DD.pdf)
  intake/<해-달>/<받은 시각>-<문서 ID>/<원래 파일명>   접수 폴더에서 옮겨 온 스캔 (watch·serve — 받은 시각은 UTC)
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
| 소프트웨어 | 본체 내장 CaptureOnTouch Lite | 저장 폴더가 곧 접수 폴더다 (`[paths] inbox` — 아래 "접수 폴더와 운영 화면") |
| 급지 폭 | 최대 216 mm | 양식은 B5 가로(257 × 182 mm)라 짧은 변부터 넣는다 — **세로로 들어온다** (90°, 뒤집으면 270°·180°). 방향은 파이프라인이 세운다 |

권장 설정 (tasks/0007 8절) — 아직 이 기기로 시험하지 않았다. 설정을 정하면 그 설정으로 스캔한 표본으로 회귀 기준을 다시 잡는다.

- PDF, 300 dpi 고정, 회색조(또는 컬러) 하나로 고정. 자동 해상도는 끈다. 이미지 파일로 저장하면 파일 하나가 문서 하나다 — 쪽마다 날짜를
  정해야 하므로 PDF 로 한다 (여러 쪽 TIFF 는 등록에서 거절한다).
- 문자 강조, 배경 제거·매끄럽게, 컬러 드롭아웃, 가장자리 강조 같은 **화질 보정은 끈다.** 켜면 잉크 판정의 기준이 달라진다.
- 기울기 보정은 켜도 된다. 자동 회전(문자 방향 인식)은 끈다 — 켜져 있어도 되지만 손글씨 양식에서는 믿을 수 없다. 방향은 첫 정합의
  호모그래피에서 읽어 세운 뒤 다시 정합한다 (tasks/0007 4.4 — 실제 83쪽 × 네 방향에서 바로 선 쪽과 정합 그림이 바이트까지 같았다).
- 단면. "빈 쪽 건너뛰기"는 끈다 — 흐리게 쓴 쪽을 빈 쪽으로 볼 수 있다. 양면으로 찍어도 양식을 못 찾은 쪽 중 거의 흰 쪽은 `blank` 로 가린다.
- 파일명은 스캐너가 붙이는 대로 둬도 된다 — 날짜 규칙에 맞지 않으면 `needs_date` 로 기다리고 화면에서 날짜를 넣는다.

## 커밋하면 안 되는 것

현장 문서에는 작업자 이름, 서명, 차량번호가 있다. 저장소에 한 번 들어가면 나중에 지워도 기록에 남는다.

- 스캔 원본과 그 일부를 잘라낸 이미지 — 검수 화면의 갈무리도 마찬가지다
- 기준 이미지(`reference.png`)와 실제 템플릿 YAML — 행렬 양식의 머리글에 이름·차량번호가 인쇄되어 있다
- 페이지 라벨, 정답 CSV·엑셀, 검수 기록(`reviews.jsonl` — 적힌 값과 출처가 들어 있다)
- 학습한 모델(`models/`), 내보낸 크롭, 틀린 칸 모아 보기 — 현장의 글씨에서 나온 것. 문서·PR·이슈에도 붙이지 않는다 (수치만 옮긴다).
  메타 필드 모델의 `classes.json` 은 이름·차량번호의 목록 그 자체다
- 인쇄 층(`print.png`)과 그것을 그린 미리보기(`template preview --print`) — 빈 양식처럼 보이지만 쪽들을 겹쳐 만든 것이라, 날마다 같은 자리에
  같은 글씨로 쓰는 칸(작성자 이름, 서명, 늘 같은 점검란)은 쪽이 많아도 잔상이 남는다. 같은 자리에 쓰는 계기 값도 쪽이 적으면 남는다.
  다른 판의 기준 이미지(`template variant`)는 현장 스캔 그 자체다. 예외는 합성 양식으로 만든 것뿐이다 (시험은 시험 중에 만든다)
- 내보낸 엑셀(엑셀 폴더의 파일, 홈에서 내려받은 파일)과 그 갈무리 — 차량번호·작성자, 인쇄된 머리글의 이름, 검수한 글자 칸, 원래 파일명이 들어 있다
- 가린 쪽 그림 — 템플릿이 아는 자리만 가렸다. 표 위의 메모, 수 칸에 적은 이름, 템플릿에 적지 않은 인쇄는 남는다.
  가렸다는 이유로 저장소·문서·PR·이슈에 넣지 않는다
- 실제 이름·차량번호를 예시로 쓴 문서·주석·테스트·커밋 메시지
- API 키와 비밀값 (`.env`, `minedocscan.toml`), 통합 DB 의 URL(`MINEDOCSCAN_PUBLISH_URL` — 비밀번호가 들어 있다)

**이름·차량번호가 들어 있는 파일** (전부 저장소 밖):

| 파일 | 위치 | 무엇이 |
|---|---|---|
| 템플릿 YAML, 기준 이미지 | `SITE/templates/<양식>/` | 행렬 머리글의 인쇄된 이름·차량번호(`header_operator`, `header_vehicle_no`) |
| 인쇄 층 | `SITE/templates/<양식>/print.png` | 늘 같은 자리에 쓰는 이름·서명의 잔상 (흐리게라도) |
| 페이지 라벨 | `SITE/labels/pages.json` | 쪽마다 차량번호·작성자 |
| 검수 기록 | `SITE/reviews/reviews.jsonl` | 입력한 차량번호·작성자 (`page-fields`, `meta-check`) |
| 메타 필드 모델의 종류 목록 | `SITE/models/<이름>/classes.json` | 고를 수 있는 이름·차량번호 (카드·학습 로그에는 없다 — 종류의 수와 분포만) |
| 내보낸 메타 크롭 | `OUT/<split>/meta/<키>/*.png`, `OUT/<split>/meta/labels.jsonl` | 글씨 그림과 그 값 |
| 틀린 칸 모아 보기 | `WORK_ROOT/recognizer-errors/` (또는 `--errors` 의 경로) | 글씨 그림과 기계·정답 값 |
| DB | `WORK_ROOT/minedocscan.db` | `doc_field`, `doc_page_meta`, `prod_haul`, `eq_assignment_obs`, `xcheck_haul`, `eq_usage_daily` 의 값, `doc_review` 의 입력값, `doc_document` 의 파일명, `eq_equipment` 의 등록번호 |
| 엑셀 | `[export] excel_dir` 의 `daily/`·`monthly/`, 홈에서 내려받은 파일 | 운반·배차·교차검증·가동 기록의 차량번호·작성자, 행렬의 인쇄된 머리글(이름·차량번호), 양식 시트의 표 밖 필드, 출처 열의 원래 파일명 |
| 통합 DB 의 표 | 현장 서버의 PostgreSQL, `[publish] schema` (기본 `minedocscan`) | 작업 DB 의 행 그대로 — `doc_field`, `doc_page_meta`, `prod_haul`, `eq_assignment_obs`, `xcheck_haul`, `eq_usage_daily` 의 값, `doc_document` 의 파일명, `eq_equipment` 의 등록번호 |
| 가린 쪽 그림 | `export masked-pages` 에 준 폴더 | 템플릿이 모르는 자리의 글씨와 인쇄 (`redact` 상자를 적지 않은 인쇄된 이름·차량번호·등록번호, 표 위의 메모, 수 칸에 적은 이름) |

`pages --meta-mismatch`, `eval --meta`, `report`, `info`, `recognizer list`·`eval` 의 출력에는 값을 찍지 않는다 (수만). 값은 검수 화면(127.0.0.1)과 내보낸 것(엑셀 폴더·내려받은 엑셀·통합 DB 의 표·가린 쪽 그림)에서만
본다 — 명령의 출력·로그에는 없다.
`export excel`·`publish` 의 요약에는 날짜와 수만, `watch`·`serve` 의 요약에는 수와 문서 ID 만 나온다. 통합 DB 는 어디서나(`publish`, `info`, 홈) 호스트·DB·스키마로만
보인다 — 사용자·비밀번호 없이, 드라이버의 오류 글 없이 (예외의 종류만).
`template print-layer`·`variant`·`add-region`·`check` 의 요약과 오류에는 칸 이름(`<표>/<열>/행 N`, `fields/<이름>`)과 수만 나온다 — 행 키는
장비 번호일 수 있어 찍지 않는다. `info` 는 장비명 대응표를 이름 대신 해시로 보여 준다.

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
(점으로 쓴 08.00 포함), 계기가 빈 장비, 대응표에 없는 장비명, 작업량 표 위의 메모가 들어 있다. 로우더 일보의 근무 시각 칸에는 "~" 가,
작업량 표의 첫 구분(두 줄)의 근무 칸에는 실제 양식의 "하단: _ 대" 처럼 라벨과 단위가 칸 안에 인쇄되어 있다 (그 칸의 값은 두 자리).
기본 합성 데이터는 그대로다.
두 선택 기능이 tasks/0006 을 시험한다 (`--usage-logs`/`--usage-only` 와 같이 쓴다):

- `--print-layers` — 가동 일보 두 종의 인쇄 층(`print.png` + `print_image`)을 넣는다. 생성기의 빈 그림이 아니라 **합성 쪽에서 추정한 것**이다
  (스캔한 쪽을 알고 있는 기하로 템플릿 좌표에 되돌려 `template print-layer` 와 같은 계산 — 날짜별로 고르게 최대 40장, 75 백분위).
  스캔과 `answers.json` 은 켜든 끄든 바이트까지 같다. 달라지는 것은 두 템플릿(`print.png`, `print_image`)과 `truth.json` 의
  `print_layers` = {양식: print_sha} 뿐이다.
- `--usage-variants` — 운행일보에 같은 날 섞여 쓰이는 판 B(`synth_usage_log_b` — 작업 표·계기 표만 10 px 아래, 줄 간격 +1 %, 머리·필드는
  판 A 와 같은 자리)를 더한다. 두 판에 `family: synth_usage_log`·`concurrent: true`, 날마다 두 판이 섞이고, 정답·truth 의 `template` 은 그 쪽의
  판 이름이다. 인쇄 층은 판마다 따로. 쪽의 내용은 판을 섞지 않은 것과 같다.

합성 팩은 `out/` 처럼 저장소 안에 만들어도 되지만(`.gitignore` 가 막는다), 템플릿 도구는 git 작업 트리 안에 쓰지 않는다:
`template print-layer`·`add-region` 은 저장소 안의 템플릿을 거절하고(합성 팩이면 `--allow-in-repo`), `template variant` 는 저장소 안의 출력
폴더를, `template preview` 는 저장소 안의 출력 폴더(기본 `WORK_ROOT/template-preview`)를 거절한다 — 이 둘에는 `--allow-in-repo` 가 없다.
이 명령들을 합성 양식으로 해 보려면 합성 팩과 WORK_ROOT 를 저장소 밖 경로에 만든다.

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
  **결정 파일은 읽는다** — 날짜와 버리기는 무엇을 적재하는가의 일부다 (ADR 0019). 그래서 기준에 든 문서에 결정을 내리면 회귀 수치가 움직인다.
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
- 파일명에 날짜가 없는 문서는 적재하지 않고 `needs_date` 로 기다린다 (요약에 한 줄, 종료 코드 0). `doc date` 로 정하면 다음 `run`·`watch`
  가 처리한다. 버리기·날짜는 결정이라 `--fresh` 로 다시 만들어도 그대로 붙는다.
- 파이프라인은 한 번에 하나만 돈다 (`run`·`watch`·`serve` — DB 옆의 `pipeline.lock`). `--fresh` 는 DB 와 `-wal`·`-shm` 을 지운다 —
  윈도우에서는 다른 프로그램(`review serve`, `serve --no-watch`)이 DB 를 열고 있으면 지우지 못하고 멈춘다 — 리눅스·맥에서는 지워지고 열고 있던 프로그램은 지워진 파일에 쓴다. 다른 프로그램을 끄고 지운다. DB(SQLite WAL)와 `WORK_ROOT` 는 로컬 디스크에 둔다
  (공유 폴더에서는 잠금이 믿을 만하지 않다).

### 접수 폴더와 운영 화면

스캐너의 저장 폴더를 접수 폴더로 둔다 (`[paths] inbox` 또는 `MINEDOCSCAN_INBOX` — `archive_root`·`work_root`·사이트 팩과 겹치면 거절한다).

```bash
minedocscan serve --reviewer jp            # 운영 화면 http://127.0.0.1:8765/ + 접수 폴더 감시 (작업 스레드)
minedocscan serve --reviewer jp --no-watch # 화면만 (결정은 남고 처리는 다른 곳의 watch 가)
minedocscan watch                          # 화면 없이 감시만.  --once: 한 바퀴 (접수 + 대기 중인 문서 처리)
minedocscan doc list [--status needs_date] # 문서 목록 — 받은 시각·쪽 수·날짜·상태·다시 처리 대기
minedocscan doc date <문서 ID|쪽 ID> 250326 --reviewer jp     # 결정을 남긴다 — 처리는 watch·serve 가
minedocscan doc discard|restore <문서 ID|쪽 ID> --reviewer jp    doc keep <쪽 ID> --reviewer jp (다시 스캔한 것이 아니다)
```

- **다 쓰인 파일만 가져온다**: 수정 시각이 `[intake] settle_seconds`(5) 지나고 열리는 파일 (계속 도는 감시는 크기가 바퀴 사이에 그대로여야).
  `give_up_seconds`(120) 지나도 열리지 않으면 손상 방침(`damaged_pdf`)대로 등록한다 — 잘린 PDF 는 `failed` 문서. 잠겨서 읽을 수조차 없으면
  `<inbox>/_failed/`. 같은 바이트의 파일은 `<inbox>/_already/` (DB 는 그대로). `_` 로 시작하는 폴더, `.`·`~` 로 시작하는 파일은 보지 않는다.
- **지우지 않는다**: 새 파일은 보관 폴더의 `intake/…/` 로 복사하고 해시를 다시 확인해 등록한 **뒤에** 접수 폴더에서 치운다. 접수 폴더에서
  사라진 파일은 보관 폴더·`_already`·`_failed` 중 한 곳에 바이트 그대로 있다. 옮기다 실패하면(윈도우: 열려 있는 파일) 다음 바퀴에 다시 한다.
- **`source_name` 은 문서마다 하나가 아니다** — 스캐너는 같은 이름을 다시 쓴다. 문서를 가리키는 것은 문서 ID 다. 라벨(`labels/pages.json`)과
  정답 파일은 파일명으로 찾으므로 날짜 있는 묶음 폴더를 위한 것이다 — **접수한 문서에는 라벨을 쓰지 않는다** (이름이 겹치면 두 문서에 같이 붙는다).
- 보관 폴더를 `run` 으로 다시 돌려도 접수로 만든 DB 와 같다 (받은 시각은 폴더 이름에서 다시 읽는다 — 문서의 순서도 같다).
- 홈: 할 일(날짜를 정할 문서, 다시 스캔 의심 쪽, 양식 없는·정합 실패 쪽, 운영 대기열마다 남은 수 — 누르면 그 대기열), 최근 문서, 작업 상태.
  문서 화면: 첫 쪽들의 그림을 보고 종이의 날짜를 넣는다 (화면이 미리 채우지 않는다. 받은 날보다 뒤·31일 넘게 앞이면 한 번 되묻는다).
  다시 스캔 의심 쪽은 두 쪽을 나란히 놓고 "같은 종이 — 이 쪽을 버린다 / 먼저 쪽을 버리고 이 쪽을 쓴다 / 다른 종이 — 둘 다 쓴다".
- 서버 로그와 감시 요약에는 경로·상태 코드, 수와 문서 ID 만 찍힌다 — 파일명·이름·날짜·메모는 화면과 내보낸 엑셀에서 본다 (로그·요약에는 없다). 화면의 갈무리를 문서·PR 에 붙이지 않는다.
- 사이트 팩(템플릿·모델·대응표)을 고친 뒤에는 `serve` 를 다시 띄운다 (돌고 있는 감시는 알아채지 못한다). 윈도우에서 켤 때 같이 띄우려면
  `minedocscan serve --reviewer <이름>` 을 부르는 바로 가기를 시작 프로그램 폴더(`shell:startup`)에 둔다 (설치 파일·서비스 등록은 M6).

### 내보내기: 엑셀 폴더, 통합 DB, 가린 쪽 그림

내보낸 것은 전부 DB 의 사본이다 — 한 방향이고, 지워도 다시 만든다 ([ADR 0022](decisions/0022-exports-are-copies.md)). 원본은 스캔 파일·검수 기록·결정
기록 셋뿐이고, 내보내기는 작업 DB 에 아무것도 쓰지 않는다. 엑셀이나 통합 DB 의 표를 고쳐도 DB 로 돌아오지 않는다 — 고치는 곳은 검수 화면이다.
**셋 다 현장의 값(이름·차량번호)을 받는다** — 저장소 밖에 둔다 (위의 표).

```bash
# 엑셀 — 설정: [export] excel_dir = "…/현장이 여는 폴더" (또는 MINEDOCSCAN_EXCEL_DIR). serve·watch 가 바퀴 끝에 쓴다
minedocscan export excel [OUT] [--date 2030-01-07 | --from … --to … | --month 2030-01]   # 손으로. 범위가 없으면 전부 훑는다 — 바뀐 파일만 쓴다
#   OUT/daily/2030-01/2030-01-07.xlsx     하루치: 요약, 양식마다 한 장(종이와 같은 행·열), 업무 표
#   OUT/monthly/2030-01.xlsx              한 달: 날짜별 요약, 운반 표, 배차, 긴 표
#   OUT/.minedocscan-export.json          기록 파일: 파일마다 내용의 해시·모델의 판·쓴 시각

# 통합 DB(PostgreSQL) — pip install -e ".[postgres]", MINEDOCSCAN_PUBLISH_URL=postgresql://사용자:비밀번호@호스트/DB (환경변수로만)
minedocscan publish --check                # 쓰지 않고 다른 범위의 수 — 같으면 0, 다르면 1, 닿지 못하면 2
minedocscan publish                        # 지문이 다른 문서·날짜만 한 트랜잭션으로 갈아 끼운다. serve·watch 도 바퀴 끝에 (엑셀 다음)
minedocscan publish --rebuild              # 이 프로그램이 만든 표만 지우고 다시 만든 뒤 싣는다 (스키마 버전·싣기의 판이 다를 때)

# 가린 쪽 그림 — 발표·보고서용. 사람이 보고 나서 쓴다
minedocscan export masked-pages OUT --date 2030-01-07 [--keep-text]     # 또는 --page-id <쪽 ID>… → OUT/<쪽 ID>.png
```

쓰는 명령(`export excel`, `publish`, `publish --rebuild`)은 파이프라인 잠금을 잡는다 — `serve`·`watch` 가 돌고 있으면 한 줄로 알리고 끝난다 (설정이
있으면 그쪽이 쓰고 있고, 엑셀은 홈에서 내려받는다. `--rebuild` 는 `serve` 를 내리고 친다). 읽기만 하는 `publish --check`·`export masked-pages` 는
돌고 있어도 된다. `run`·`serve --no-watch` 는 내보내지 않는다.

**엑셀 폴더**

- **들어가는 것**: 양식 시트(종이와 같은 행·열, 표 밖 필드의 차량번호·작성자까지), 업무 시트(운반·배차·교차검증·가동 기록·작업량·계기 검산·점검 —
  차량번호·작성자, 행렬의 인쇄된 머리글, 점검내역), 출처 열의 원래 파일명(`파일명#쪽`)과 쪽 ID·필드 ID. 칸은 **확정된 값만** — 검수 대기는 `?`,
  사람이 읽지 못한 칸은 `판독 불가`, 합계는 그 줄이 전부 확정일 때만 (ADR 0022). 날짜를 정할 문서·실패한 문서는 없다 (홈에서 본다).
- **저장소 밖에.** `OUT`(설정의 `excel_dir`)이 git 작업 트리 안·접수 폴더 안·보관 폴더 안이면 거절한다. `export excel` 명령만 합성 데이터일 때
  `--allow-in-repo` 로 저장소 안에 쓸 수 있다 (설정의 `excel_dir` 에는 예외가 없다). 설정이 그렇다면 `serve`·`watch` 가 시작할 때 한 줄로
  알리고 자동 내보내기를 켜지 않는다.
- **폴더는 있어야 한다** — 없으면 만들지 않고(끊긴 네트워크 폴더의 자리에 로컬 폴더를 만들지 않게) "폴더가 없습니다"로 알린다. 처리는 계속되고
  다음 바퀴에 다시 본다. 그 아래의 `daily/…`·`monthly/` 는 만든다.
- **열려 있는 파일은 다음 바퀴에.** 같은 폴더의 임시 이름(`.xlsx` 가 아닌 것)에 쓰고 바꿔 넣는다 — 덜 쓰인 `.xlsx` 는 보이지 않는다. 바꾸지
  못하면(윈도우: 엑셀이 그 파일을 열고 있다) 임시 파일을 치우고 "쓰지 못함"으로 센 뒤 다음 바퀴(명령이면 다음 실행)에 다시 한다 — 다른 이름으로
  쓰지 않는다. 열어 둔 파일은 닫을 때까지 예전 내용이다. 홈과 감시 요약에 쓰지 못한 파일의 수가 나온다.
- **바뀐 것만 쓴다** — 내용(모델)의 해시가 기록 파일과 같으면 파일을 건드리지 않는다 (수정 시각 그대로 — xlsx 의 바이트는 같은 내용이어도 쓸 때마다
  다르다). `serve`·`watch` 는 처리·검수·결정이 건드린 날짜(계기의 연속성으로 번지는 같은 장비의 다른 날짜까지)와 그 달만 다시 보고, 시작할 때와
  `[export] sweep_minutes`(30)마다 전부 훑는다 — 다른 프로세스(`review serve`)가 저장한 검수는 이 훑기에서 따라온다.
- **지워도 된다.** 파일을 지우면 다음 전체 훑기(또는 `export excel`)에 다시 쓴다. 기록 파일까지 지웠으면 전부 다시 쓴다. 엑셀 폴더 자체를
  지웠으면 사람이 다시 만든다 — 프로그램은 만들지 않는다 (위). 만들면 다음 바퀴에 전부 다시 쓴다 (기록 파일도 없으므로). 쪽이 하나도
  남지 않은 날짜(문서를 버렸다, 날짜를 옮겼다)·달의 파일은 이 프로그램이 지운다 — 기록 파일에 있는 것만.
- **사용자가 둔 파일은 지우지 않는다.** 이름 규칙(`daily/<YYYY-MM>/<YYYY-MM-DD>.xlsx`, `monthly/<YYYY-MM>.xlsx`)에 맞지 않는 파일은 건드리지 않는다.
  규칙에 맞는 이름은 이 프로그램의 것이다 — 그 자리의 파일을 고쳐 두면 내용이 바뀔 때 덮어쓴다. 고친 엑셀은 다른 이름·다른 폴더에 둔다.
  기록을 잃었으면 규칙에 맞는 파일이라도 지우지 않고 수로 알린다.
- **정답을 만드는 사람은 엑셀을 보지 않는다** (눈가림 — [ADR 0008](decisions/0008-review-records.md)). 엑셀에는 자동 적재된 기계 값과 쪽 메타가
  확정 값으로 보이고, `[export] machine_values = true` 면 검수 대기 칸의 기계 값까지 업무 시트·긴 표의 "기계 값(확정 아님)" 열에 나온다 (기본은 끔).
  정답을 만드는 대기열(`haul-numbers`, `mismatch`, `page-fields`·`--audit`, `readings`, `checks`)은 기계 값을 숨긴다 — 엑셀을 옆에 띄우면 그것이
  깨진다. 정답은 대기열에서만 넣는다.
- 홈의 내려받기(`/export/day.xlsx?date=…`, `/export/month.xlsx?month=…`)는 같은 내용을 그때 만들어 준다 — 엑셀 폴더 설정이 없어도 되고 서버는
  디스크에 쓰지 않는다. 내려받은 파일은 브라우저의 내려받기 폴더에 남는다 — 같은 현장 데이터다.

**통합 DB**

- **대상은 환경변수 `MINEDOCSCAN_PUBLISH_URL` 로만 받는다** — `minedocscan.toml` 의 `[publish]` 에 URL·비밀번호를 적으면 설정 오류다. 설정 파일에는
  `schema`(기본 `minedocscan`, 환경변수 `MINEDOCSCAN_PUBLISH_SCHEMA` — 영문 소문자·숫자·밑줄), `enabled`(URL 이 있으면 켜짐), `sweep_minutes`(30),
  `connect_timeout_s`(5), `lock_timeout_s`(5), `statement_timeout_s`(60), `retry_seconds`(60). URL 은 어디에도 찍히지 않는다 — `info`·`publish`·홈에는
  호스트·DB·스키마만.
- **전용 계정으로 그 스키마에만 쓴다** (관리자 계정을 쓰지 않는다). 관리자가 스키마를 만들어 그 계정에 소유를 주면(`CREATE SCHEMA … AUTHORIZATION
  <전용 계정>`) 그 계정에는 DB 의 `CREATE` 권한이 필요 없다 — 처음 싣기(대상에 `pub_meta` 가 없을 때)와 `--rebuild` 는 스키마가 **없을 때만**
  `CREATE SCHEMA` 를 보내고, 그 안에 표와 상태 표(`pub_state`, `pub_meta`)를 만든다. 그 뒤의 싣기는 그 스키마의 표에만 쓴다. 이 스키마의 표는 이 프로그램이 갈아 끼운다 — 2단계는 읽기만
  하고 자기 표·뷰는 따로 둔다. 싣기는 상태 표의 지문과 견주므로 대상에서 고친 행을 알아채지 못한다 (그 범위가 바뀔 때까지 틀린 사본으로 남는다).
  `--rebuild` 는 이 프로그램이 만든 표만 지우고 다시 만든 뒤 싣는다 — 한 트랜잭션이라 읽는 쪽은 빈 표를 보지 않고, 실패하면 대상은 그 전 그대로다.
  뷰가 걸려 있으면 지우지 않고 멈춘다.
- **이름·차량번호가 간다** — 현장의 서버라는 전제다. 싣는 표 12개(`doc_document`, `doc_page`, `doc_field`, `doc_page_meta`, `eq_equipment`,
  `eq_assignment_obs`, `insp_daily`, `prod_haul`, `prod_tally`, `eq_usage_daily`, `xcheck_haul`, `xcheck_usage`)의 행은 작업 DB 그대로다: 쪽 메타·운반·
  배차·교차검증·가동 기록의 차량번호·작성자, 장비 마스터의 등록번호(`eq_equipment.registration`), 원래 파일명(`source_name`)과
  보관 폴더 기준 경로(`source_rel`), 검수자(`reviewed_by`), 기계 값과 검수 대기인 값까지 (엑셀처럼 빼지 않는다 — 확정인지는
  `doc_field.review_status` 로 본다). 빼는 열은 다섯뿐이다: `created_at`, `received_at`, `work_requested`, `work_done`, `source_path`
  (검수 시각 `reviewed_at`, 정합 그림의 상대 경로 `aligned_image` 는 간다). 빼는 표: `doc_review`·`doc_decision`(원본은 파일이다),
  `doc_page_sig`, `meta_schema`.
- **서버가 꺼져 있으면** `serve`·`watch` 는 그 바퀴의 싣기만 건너뛰고 접수·처리·엑셀을 계속한다. 건드린 범위는 들고 있다가 다음에 같이 싣고,
  싣기가 실패한 뒤(연결·권한·판 …) `retry_seconds` 동안은 다시 연결하지 않는다. 홈에는 밀린 범위의 수와 마지막 실패의 종류, 감시 요약에는
  "통합 DB: 싣지 못함 (…, 밀린 범위 N) — 다음에 다시". 서버가 돌아오면 다음 바퀴에 따라온다 (아직 한 번도 훑지 못했으면 전부 훑는다). `publish` 명령은 한 줄(호스트·DB 와
  예외의 종류)과 종료 코드 2.
- **대상이 기다리게 하면** — 다른 연결이 실은 표의 행을 쥐고 있다(DB 도구에서 고치고 커밋하지 않았다, 2단계가 표에 인덱스·제약을 거는 중 …) — 싣기는
  `lock_timeout_s`(5초) 뒤에 그만둔다 (`lock_timeout`). 문장 하나가 `statement_timeout_s`(60초)를 넘어도 그만둔다 (`statement_timeout`). 둘 다 위와 같이 그
  바퀴의 싣기만 건너뛰고 `retry_seconds` 뒤에 다시 한다 — 쥔 쪽이 놓을 때까지 접수는 `retry_seconds` 마다 그만큼씩 늦는다. 싣는 가운데 서버가 꺼지면
  TCP keepalive 로 끊는다 (`connection_lost` — 윈도우는 약 30초, 리눅스는 두 시간 제한 중 긴 것 + 30초 = 기본 90초). 싣는 동안 홈의 작업 상태는
  "통합 DB 에 싣는 중 — N초째"다. 실은 표를 고치려면 이 프로그램을 멈추고 하거나
  자기 표·뷰에서 한다 (실은 표에는 이 프로그램만 쓴다 — ADR 0021).
- **한 번의 싣기는 한 트랜잭션이다** — 중간에 끊기면 대상은 싣기 전 그대로이고, 읽는 쪽은 반쯤 바뀐 문서를 보지 않는다. `psycopg` 가 없으면
  `publish` 는 한 줄과 종료 코드 2, `serve`·`watch` 는 시작할 때 한 번 알리고 싣기를 끈다.
- 대상의 스키마 버전이나 싣기의 판이 이 프로그램과 다르면 싣지 않고 `publish --rebuild` 를 알린다. 실을 수 없는 값(NUL 문자가 든 글자 …)이 있는
  범위(문서·날짜)는 싣지 않고 대상의 옛 행을 지운 뒤 수로 알린다. 범위·지문·불변식은 [ADR 0021](decisions/0021-publish-to-the-shared-db.md).

**가린 쪽 그림**

- **템플릿이 아는 자리만 가린다.** 정합 그림(템플릿 좌표) 위에서 서명(`kind: signature` 인 표 밖 필드와 표의 칸), 가릴 메타 키의 필드(기본 `operator`·`vehicle_no` —
  `site.toml` 의 `[redact] meta_keys`), 템플릿의 `redact` 상자(결재란, 행렬 머리의 인쇄된 이름, 점검표의 인쇄된 등록번호 열 — 판마다 따로 적는다,
  [SITE_PACK.md](SITE_PACK.md)), 글자 칸 전부(표의 `handwritten_text` 열은 괘선까지, 표 밖의 글자 필드)를 한 색(검정)으로 채운다 — 흐리게 하지
  않는다. 표 밖 필드와 `redact` 상자는 `[redact] pad_px`(기본 16 px)만큼 넓혀서. 수 칸·✓ 칸·인쇄는 남는다.
- **남는 것**: 표 위에 걸쳐 쓴 메모, 수 칸에 적은 이름, `redact` 상자를 적지 않은 인쇄(행렬 머리의 이름·차량번호는 상자를 적어야 가려진다),
  넓힌 폭보다 더 넘어간 글씨. `--keep-text` 면 글자 칸과 `meta_key` 가 없는 이름 필드도 남는다. 그래서 **사람이 한 장씩 보고 나서 쓴다**
  (명령의 요약도 그렇게 말한다).
- 기본 `pad_px` 16 은 합성에서 잰 것이다 (합성 표 밖 필드 206개에서 글씨가 상자를 넘은 거리: 최대 11 px, 99 % 9 px). 실제 글씨는 더 넘을 수 있다 —
  양식마다 한 쪽씩 내어 보고 현장의 값을 `site.toml` 에 적는다.
- 적재된 쪽만 낸다. 파일 이름은 쪽 ID 다 (원래 파일명을 쓰지 않는다). 정합 그림이 없으면(`[pipeline] save_aligned = false`) 원본을 호모그래피로
  다시 편다 — 원본에 닿지 않는 쪽은 내지 않고 수로 알린다. 요약에는 쪽 수와 OUT, 종류별로 가린 상자의 수,
  내지 않은 쪽의 수(이유별), 넓힌 폭만 나온다 — 이름·값·원래 파일명은 없다.
- **저장소 밖에.** 명령은 git 작업 트리 안의 `OUT` 을 거절한다. 가린 그림도 현장 데이터다 — 가렸다고 저장소·문서·PR·이슈에 넣어도 되는 것이 아니다.
- 운영 화면(홈·문서 화면)의 그림은 가리지 않는다 — 127.0.0.1 에서 검수하는 사람이 본다.

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

가동 일보(중기운행일보·점보·로우더 작업일보)의 템플릿을 만드는 순서와 입력 (tasks/0005·0006, ADR 0015–0018).
칸을 정하기 전에 **분류 전용으로 먼저 돌려 인쇄 층을 만들고**, 그 위에서 표를 잡는다. 템플릿 형식의 자세한 것과 합성 양식으로 해 보는 예는
[SITE_PACK.md](SITE_PACK.md) 에 있다.

```bash
# 1. 분류 전용 템플릿으로 전체 묶음을 돌린다 — 쪽은 classified_only (정합도 칸도 없다)
minedocscan template init <깨끗한 쪽 PDF> --name <양식> --handler usage   # 그 뒤 template.yaml 을 regions: [] , fields: [] 로
minedocscan run DB_scans --fresh

# 2. 인쇄 층 — 분류 전용 쪽은 명령이 직접 정합한다
minedocscan template print-layer <site>/templates/<양식>                 # 판이 섞였을 수 있는 양식이면 --percentile 50 (괘선을 잡는 데만)
#    template.yaml 에 print_image: print.png 를 적는다 (명령은 template.yaml 을 고치지 않는다).
#    50 의 층이면 3 을 마친 뒤 그 줄을 지우고 돌리고, 판을 나눈 뒤 판마다 75 로 다시 만들어 다시 적는다
minedocscan template preview <site>/templates/<양식> --print             # 층 그대로의 템플릿 좌표 — 표마다 --roi 를 여기서 읽는다

# 3. 표를 더하고 채운다
minedocscan template add-region <site>/templates/<양식> --roi x0,y0,x1,y1 --name meter --role meter   # 표마다 한 번
#    열 이름·kind·format, 행 키·메타, 필드(장비명·운전자에 meta_key)를 채운다. 인쇄가 든 칸은 떼어 내지 않고 그대로 둔다
minedocscan template check   <site>/templates/<양식>                     # 통과할 때까지 이 사이트 팩으로 run 하지 않는다
minedocscan template preview <site>/templates/<양식> --print
minedocscan run DB_scans --fresh                                        # 스키마 7

# 4. 같은 날 섞여 쓰이는 다른 판이 있으면
minedocscan pages --status align_failed --template <양식> --thumbs      # 실패한 쪽과 미리보기 (WORK_ROOT/thumbs)
minedocscan template variant <site>/templates/<양식> --scan <PDF> --page N --name <양식>_b
#    기존 판의 template.yaml 에 family 와 concurrent: true (새 판에는 명령이 적었다 — 기존 판에 적을 줄을 안내한다.
#    적기 전에는 사이트 팩이 읽히지 않는다: 계열에 동시 판이 하나뿐)
minedocscan template preview <site>/templates/<양식>_b --scan <PDF> --page N   # 표 밖 필드의 bbox 는 기존 판 그대로다 — 맞는지 본다
minedocscan run DB_scans --fresh
minedocscan template print-layer <site>/templates/<양식>_b               # B 로 적재된 쪽으로 → B 의 template.yaml 에 print_image
minedocscan template print-layer <site>/templates/<양식>                 # A 도 A 로 적재된 쪽만으로 다시 (기본 백분위)
minedocscan run DB_scans --fresh
minedocscan report                                                     # 동시 판: 판마다 고른 쪽, 정합 실패, 고른 쪽 중 오차 차이 1 px 미만인 쪽
minedocscan pages --variants                                           # 가르기 어려웠던 쪽과 판마다의 괘선 오차

# 5. 기준을 갱신한다
minedocscan regress --update                                           # 가동 일보 양식의 항목만 달라져야 한다

# 6. 입력
# site.toml [equipment.aliases]: 일보에 적는 이름 → 점검표의 장비 키 (모르는 것은 비워 둔다). 고친 뒤에는 다시 돌린다 (아래)
minedocscan review serve --queue page-fields --reviewer jp      # 장비명·운전자 (후보 = 대응표의 이름 + 라벨·검수에 나온 값)
minedocscan review serve --queue readings --reviewer jp         # 가동 시간 칸: 쪽마다 계기(시작·종료·총) + 근무 시각을 한 번에
minedocscan report                                              # 가동 기록: 계기 칸의 종류별, 가동 시간의 근거별, 검산(이어짐 / 어긋남 …)
minedocscan review serve --queue usage-check --reviewer jp      # 계기가 이어지지 않는 곳: 어제의 종료 칸과 오늘의 시작 칸을 같이
minedocscan review stats                                        # 형식별·대기열별
```

**인쇄 층** (ADR 0017 — 무엇에 쓰고 무엇에 쓰지 않는지는 [ARCHITECTURE.md](ARCHITECTURE.md) §5)

- **5장 이상으로 만든다.** 명령은 5장 미만이면 경고하고 만든다 (3장 미만은 거절). 쪽이 적으면 여러 쪽의 같은 자리에 쓴 값(계기 값, 작업량)이
  층에 잔상으로 남고, 그 자리에 쓴 값까지 지워 빈 칸으로 자동 적재될 수 있다 — 합성에서 2장으로 만든 로우더 층이 값이 적힌 작업량 칸 3칸을
  잃었다 (백분위를 보간 없이 잡아 3–5장은 잃지 않았다). 잔상은 `add-region` 에서 없는 세로 괘선으로도 잡힌다. 쪽이 모자란 판에는 키를 아직 적지 않고, 쪽이 쌓인 뒤
  만든다 — 인쇄 층이 없어도 값은 사라지지 않는다 (인쇄가 든 칸이 검수로 갈 뿐이다).
- **판이 섞였을 수 있는 양식의 첫 층은 `--percentile 50` — 괘선을 잡는 데만 쓴다.** 판을 나누기 전에는 두 판의 쪽이 한 층에 들어간다.
  75 백분위는 쪽의 75 % 넘게 어두운 화소만 남기므로, 소수 판의 몫이 25 % 를 넘으면 다수 판의 표 괘선이 층에서 빠지고 `add-region` 이 괘선을
  못 잡는다. **값 유무에 쓰는 층(`print_image` 를 적고 `run`)은 판을 나눈 뒤 판마다 75 로**, 그 판으로 적재된 쪽만으로 다시 만든다 — 낮은
  백분위의 층에는 같은 자리에 쓴 값의 잔상이 더 남고, 첫 층에는 다른 판의 쪽이 섞여 있었다. 75 미만이면 요약이 그렇게 경고한다.
  50 의 층은 한 판이 쪽의 절반을 넘을 때만 그 판의 괘선을 남긴다 (보간하지 않는다 — 두 판이 꼭 반씩이면 `--max-pages` 를 홀수로).
- **요약과 `preview --print` 를 본다.** 요약의 "인쇄에 덮인 손글씨 칸"은 칸 안의 인쇄이거나 잔상이다. 표 칸에 잔상이 없어야 한다 — 필드의
  잔상은 상관없다 (필드는 인쇄 층을 쓰지 않는다). `template check` 는 표의 손으로 쓰는 칸 중 인쇄 마스크가 절반 넘게 덮은 칸을 오류로 알린다
  (값이 들어갈 자리가 없다).
- `print_image` 키는 파일을 만든 뒤에 사람이 적는다 — 키가 먼저 있으면 템플릿 오류로 사이트 팩 전체가 읽히지 않는다. 층을 다시 만들면
  해시(`print_sha`)가 바뀐다 → `run --fresh` (`--skip-existing` 은 안 된다). `info` 가 템플릿마다 인쇄 층과 해시, 쓰이는지를 보여 준다.

**표 더하기와 다른 판**

- `add-region --roi` 는 템플릿 좌표(기준 이미지 픽셀)다. 표 둘레를 조금 넉넉히, 이웃 표까지의 간격보다는 좁게 잡는다 (합성 두 양식은 표 둘레
  20 px 에서 맞는다. 표 사이가 50 px 인 합성 운행일보에서는 60 px 이면 이웃 표의 괘선이 들어온다). 요약의 괘선 수를 인쇄된 표와 맞춰 본다 — 많으면 잔상이다
  (쪽이 쌓인 뒤 층을 다시 만들거나 그 괘선을 지운다). 인쇄 층이 없으면 기준 이미지에서 잡는데, 채워진 스캔이면 손글씨의 세로획이 괘선으로 섞인다.
- `--role` 의 자리표시(meter: `start`·`end`·`total`, shifts: `range`, tally: 정수 열)가 맞는 열에 붙었는지는 `template check` 가 보지 않는다 —
  요약의 자리표시 목록을 `preview --print` 의 머리글과 맞춰 본다. 인쇄되지 않은 나눔 선(`split_ys`)은 잡지 않는다 — 사람이 적는다.
- 다른 판(`template variant`)은 `align_failed` 쪽 가운데 머리·제목이 잘 보이는 깨끗한 쪽으로 만든다. 새 판에는 `family`(기존 판의 것, 없으면
  기존 판의 이름)와 `concurrent: true` 가 적히고 `print_image` 는 없다. 명령은 기존 판의 파일을 고치지 않고 거기 적을 줄을 안내한다 —
  적기 전에는 사이트 팩이 읽히지 않는다 (계열에 동시 판이 하나뿐 — 오류가 적을 판을 말한다).
  짝이 없는 괘선이 있는 표, 인쇄되지 않은 나눔 선이 있는 표는 기존 괘선 그대로이고 "사람이 고칠 것"으로 나온다.
- 동시 판끼리는 기하(괘선·필드의 bbox) 밖의 전부 — handler·handler_options, 표·`header_rows`·열·행(메타까지)·필드 — 가 같아야 사이트 팩이
  읽힌다. 나중에 한 판에 표를 더하거나 메타를 고치면 다른 판도 같이 고친다
  (`add-region` 이 알린다). 다른 양식에도 섞인 판이 있는지는 `report` 의 양식별 괘선 오차와 `pages --template <양식>` 의 쪽마다 괘선 오차로
  본다 — 몇 쪽만 크게 떨어져 있으면 다른 판이다.

**입력**

- **`readings` 는 가동 시간을 정하는 칸의 대기열이다.** 항목 = 쪽 하나: 계기 칸(시작·종료·총) 다음에 근무 시각 칸(`shifts` 표의 `time_range` 칸,
  행 순서 — 라벨은 행에 인쇄된 근무 구분). 그중 하나라도 기계가 잉크를 본 쪽이 올라온다 — 계기가 비고 근무 시각만 적힌 쪽(로우더)도
  한 번의 저장으로 가동 시간이 근무 시각 근거(`shifts`)로 정해진다. 항목의 칸이 전부 검수되어야 끝난다 — 계기 칸만 검수한 쪽은 근무 시각
  칸 때문에 다시 올라온다. `--audit N` 은 잉크와 상관없이 날짜별로 고르게 N 쪽 — 빈 칸으로 넘어간 칸을 잴 정답.
- 작업량 칸의 검수 대기는 지금처럼 `pending`(`--template`, `--kind` 로 좁힌다)이다. 계기·근무 시각 칸도 같은 `handwritten_number` 라 거기
  같이 나오지만 그 칸은 `readings` 에서 넣는 것이 빠르다.
- **계기 칸의 규칙**: 계기 값은 숫자 그대로(1234.5), 시각은 콜론으로(08:00) — 점으로 쓴 시각(08.00)도 콜론으로 입력. 숫자인지 시각인지는 사람이 정한다
  (코드는 콜론만 본다). 근무 시각 칸은 범위로("8-17", "08:00~17:00"). 빈 칸은 비워 두고 `Enter`. 형식에 맞지 않는 값(12:75, 1234,5)은 화면과
  서버가 거절하고 검수 파일에 남지 않는다.
- **점으로 쓴 시각은 화면이 묻는다.** 적힌 대로 08.00 을 넣으면 계기 값 8.00 이 되고, 첫날에는 검산에도 걸리지 않는다. 그래서 계기 표의
  시작·종료 칸(`reading` 형식)에 소수 두 자리이고 24:00 이하이며 소수부가 00–59 인 값(08.00, 17.30)을 넣으면 저장 전에 묻는다 —
  `1` 시각(`08:00` 으로 저장), `2` 계기 값(그대로 저장), `Esc` 돌아가서 고쳐 쓰기. `readings`·`usage-check`·`pending` 어디서든 같다.
  `1234.5`, `8.5`, `25.30`, `08.75` 와 총 칸·근무 시각 칸은 묻지 않고(총은 가동 시간 — 길이다), 이미 정한 값(전의 검수, `usage-check` 의
  저장된 값)을 그대로 저장할 때도 묻지 않는다. `pending` 이 미리 채운 기계 값은 정한 값이 아니라서 묻는다.
  물었다는 것은 검수 파일에 남지 않는다 — 아래 "돌려줄 수치"의 물은 횟수는 검수자가 센다.
- `readings` 는 **기계 값도 앞날의 값도 보여 주지 않는다** — 보여 주면 그 값을 따라 적는다. 어제와 오늘을 같이 보는 것은 `usage-check` 에서만.
- `usage-check`: 종이와 다르게 입력된 칸만 고치고 `Enter` — 검산이 맞게 되면 빠진다. 한 칸을 고쳐도 여전히 어긋나면 남는다.
  아무것도 고치지 않고 `Enter` 하면 "종이에 적힌 대로"를 확인한 것이고 끝난다 — 어긋남은 `xcheck_usage` 와 리포트에 그대로 남는다
  (값을 맞춰 넣지 않는다, ADR 0006). 빠진 날이 있는 장비의 `gap` 은 확인하고 넘어가는 것이 맞다.
- 장비명을 고치면 예전 장비와 새 장비 양쪽의 계기 검산이 바로 다시 계산된다.
- **`[equipment.aliases]` 를 고친 뒤**: `info` 의 대응표 해시가 바뀐다. 장비 ID 는 쪽을 적재할 때(와 그 쪽의 칸을 검수해 쪽의 행을 다시 만들
  때) 정해지므로 다시 돌리기 전에는 예전 ID 가 남는다 — `report` 가 지금의 대응표와 다른 행의 수를 한 줄로 알린다. `run --fresh` 또는 그 문서를
  다시 돌리면 사라진다.

**리포트의 줄** (값·이름 없이 수만)

| 줄 | JSON 키 | 뜻 |
|---|---|---|
| `인쇄 층으로 값 유무를 잰 쪽: <양식> N …` | `report.print_layer` | 양식별로 인쇄 층으로 잰 적재된 쪽 |
| `동시 판 <계열>: 고른 쪽 …, 정합 실패 N, 고른 쪽 중 두 판의 괘선 오차 차이가 1 px 미만인 쪽 N` | `report.variants` | 판마다 고른 쪽, 통과한 판이 없던 쪽, 고른 쪽 중 가르기 어려웠던 쪽 (`pages --variants` — 정합 실패 쪽도 상태와 함께) |
| `계기 값의 시작·종료가 둘 다 24 이하인 쪽 N …, 계기 값과 시각이 섞인 쪽(mixed) N` | `report.usage_dotted_suspect` | 시각을 숫자로 넣었을 수 있는 쪽 (세기만 한다 — 종이와 대 본다) |
| `장비 ID 가 지금의 대응표([equipment.aliases])와 다른 행: …` | `stale_equipment_ids` (`report` 밖) | 대응표를 고친 뒤 다시 돌리지 않은 가동 기록·작업량의 행, 그 쪽·문서 수 (사이트 팩이 있고 가동 기록·작업량 행이 있을 때만) |

- 앞의 둘은 해당하는 쪽이 있을 때만, 셋째는 가동 기록이 있으면(0 이어도) 생긴다. 셋 다 리포트 묶음 안이라 `regress` 가 비교한다 — 처음 생긴
  사이트는 `regress` 에 새 항목으로 나오므로 확인하고 `regress --update`. 인쇄 층·동시 판·가동 기록이 없는 사이트의 리포트와 기준은
  예전과 같다. 마지막 줄은 사이트 팩에 따라 달라지는 수라 비교하지 않는다.

**돌려줄 수치** (이름·번호 없이 수치만)

- tasks/0005 8절: 양식별 쪽 수와 정합 통과율, 계기 칸이 있는 쪽 / 시각 / 빈 쪽, 장비 수, 연속성 검산의 결과별 수와 어긋난 것의 원인
  (빠진 날 / 잘못 적음 / 다른 장비), 한 시간에 입력한 쪽 수, 작업량 표에 값이 있는 칸의 비율.
- tasks/0006 8절: 양식별로 인쇄 층을 만든 쪽 수(요약의 "쪽 N장"), 인쇄 층을 켜기 전과 뒤의 검수 대기 칸 수(계기·근무 시각·작업량),
  `readings` 에 값 없이 올라온 쪽 수(올라왔는데 칸을 전부 빈 칸으로 넣은 쪽), 판마다 고른 쪽 수와 `align_failed` 의 수, 두 판의 오차 차이가
  1 px 미만인 쪽 수 (`report` 의 동시 판 줄), 섞인 판이 더 발견된 양식, 점으로 쓴 시각을 물은 횟수 (검수자가 센 것 — 세지 못했으면 "재지 않음").
- 켜기 전과 뒤의 검수 대기 칸은 예를 들어 표를 채운 뒤 `print_image` 줄을 잠시 막고 `run --fresh` 한 번, 되돌리고 `run --fresh` 한 번으로 잰다.
  기계가 정한 상태(`status_raw` — 검수해도 바뀌지 않는다)로 세면 검수가 섞이지 않는다 (WORK_ROOT 의 SQLite DB):
  ```sql
  SELECT p.template_name, f.region, COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id
  WHERE f.status_raw = 'pending' AND f.region <> 'fields' GROUP BY p.template_name, f.region;
  ```

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
