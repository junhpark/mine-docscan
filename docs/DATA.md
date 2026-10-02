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
| 모델 가중치 | 로컬 또는 모델 저장소 | 아니오 |

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
- 실제 이름·차량번호를 예시로 쓴 문서·주석·테스트·커밋 메시지
- API 키와 비밀값 (`.env`, `minedocscan.toml`)

`.gitignore` 가 이미지·PDF·엑셀·CSV·DB·`sites/`·`work/` 를 기본으로 막는다(`tests/fixtures/` 만 예외).
테스트에 이미지가 필요하면 `tools/synth.py` 로 만든다. 문서의 예시는 합성 데이터의 값(`T01`, `V-101`, `ALPHA`)을 쓴다.

## 시험 데이터 두 가지

### 합성 데이터 — 저장소만 있으면 된다

`minedocscan synth <폴더>` 또는 테스트의 `synth` 픽스처. 가상의 양식 세 종에 무엇을 적었는지 알고 있으므로
분류·정합·체크 판정·값 유무·교차검증·배차 관측을 정답과 정확히 비교할 수 있다.

```
<폴더>/site/           합성 사이트 팩
<폴더>/scans/          하루에 PDF 한 개 (점검표 → 차량별 일보 → 행렬)
<폴더>/truth.json      날짜별 정답과 기대 수치
<폴더>/answers.json    오라클 백엔드·eval 용 정답
```

한계: 글자가 영문 내장 글꼴이다. 이 데이터로 **한글 손글씨 인식률을 말할 수 없다.** 잴 수 있는 것은 기하와 논리다.
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

### 검수값으로 평가하기

운반 횟수의 정답은 검수 도구로 만든다 (ADR 0007). 검수 기록은 사이트 팩의 `reviews/reviews.jsonl` 에 쌓이고
(한 줄 = 한 건, 추가 전용), DB 를 지우고 다시 돌려도 파이프라인이 시작할 때 읽어 들여 그대로 붙는다.

```bash
minedocscan review serve --queue haul-numbers --n 1500 --reviewer jp   # 표본 1,500셀(빈 칸 10 %), 브라우저 127.0.0.1:8765
minedocscan review serve --queue mismatch --reviewer jp                 # 교차검증 불일치 칸: 두 문서의 셀을 같이 본다
minedocscan review serve --queue pending --template <양식> --reviewer jp  # 운영용: 기계 값을 미리 채워 준다
minedocscan review stats                                                # 판정·양식·날짜·검수자별 건수
minedocscan review export-answers answers.json                          # value/empty 검수 → 정답 파일 (illegible 제외)
minedocscan run DB_scans --fresh                                        # 새 인식기로 다시 돌린다. 검수값은 그대로 붙는다
minedocscan eval --answers answers.json --target raw --only-listed      # 기계가 읽은 값(value_raw)을 검수값과 비교
minedocscan report                                                      # 횟수 일치율: 최종 값 기준 / 기계 값 기준
```

- `--target raw` 를 써야 한다. `final` 은 검수값 자신이라 언제나 맞는다.
- `--only-listed`: 표본만 검수했으므로 정답에 있는 셀만 평가한다. 없으면 그 표의 나머지 셀을 빈 칸 정답으로 친다.
- 결과에는 정답이 빈 칸인 셀과 값이 있는 셀의 정확도가 따로 나오고, 값 유무 판단의 정밀도·재현율이 같이 나온다.
- 검수 파일은 사람이 입력한 유일한 데이터다. 사이트 팩과 함께 백업한다. 한 번에 한 사람만 입력한다.
- 검수자가 한 사람이면 그 사람의 오독이 정답에 들어간다. 표본의 일부는 두 번째 사람이 따로 입력해 일치도를 본다 (아직 도구에 없다).

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

점검표 CSV — 기존 연구팀 형식. 파일명 `YYMMDD.csv`, cp949, 머리 3줄 뒤에 양식의 행 순서대로 `구분,형식,등록번호,점검내역`.

## 평가셋을 키울 때

- 스캔과 정답의 **기간이 겹쳐야** 한다. 지금 가진 스캔 묶음과 현장 입력 엑셀은 기간이 달라서 엑셀을 정답으로 쓰지 못하고,
  같은 기간의 엑셀은 구할 수 없다. 숫자 정답은 검수 도구로 직접 만든다 (ADR 0007).
- 사람이 검수 도구에서 입력하거나 고친 값(`reviewed`)이 그대로 정답이 된다. 따로 라벨링 작업을 만들지 않는다.
- 스캔 원본은 300 dpi 다. 파이프라인은 200 dpi 로 렌더링해서 쓰므로, 인식기 학습용 크롭을 내보낼 때는 어느 해상도인지 같이 기록한다.
- 인식 백엔드를 비교할 때는 같은 평가셋, 같은 지표, 같은 전처리(정합된 셀 크롭)로 비교한다.
