# mine-docscan

광산 현장의 수기 문서(일일 점검표, 작업일보 등)를 스캔하면 양식을 알아보고, 칸마다 값을 뽑아,
출처와 함께 데이터베이스에 적재하는 파이프라인입니다. 패키지와 명령 이름은 `minedocscan`, 판은 1.0.0 입니다
(바뀐 것은 [CHANGELOG.md](CHANGELOG.md)). 쓰는 법은 [사용 설명서](docs/manual/README.md)에 있습니다.

```
스캔 PDF/이미지 → 양식 분류 → 정합 → 셀 추출 → 인식 → 교정 → 검증 → 적재 → 양식 간 교차검증
                                              (플러그인)  (플러그인)          (DB)      (불일치를 보여 준다)
```

- **양식은 코드가 아니라 데이터입니다.** 양식 하나는 YAML 정의와 기준 이미지 한 장이고, 현장별 묶음(사이트 팩)은 저장소 밖에 둡니다.
- **인식 모델 없이도 끝까지 돕니다.** 분류·정합·체크 판정·값 유무·교차검증은 영상처리와 규칙만으로 하고,
  손글씨를 읽는 부분만 교체 가능한 백엔드로 분리했습니다.
- **모든 값에 출처가 남습니다.** 어느 문서 몇 쪽의 어느 좌표에서 나왔는지, 원문과 최종값, 신뢰도, 검수 상태.
- **맞지 않는 값은 숨기지 않습니다.** 같은 운반 횟수가 두 양식에 적히면 둘을 비교해 불일치를 날짜별로 보여 줍니다.
  장비 가동 일보의 계기는 같은 장비의 어제 종료와 오늘 시작을 비교합니다.

## 설치

- **현장 PC (윈도우, 외부망 없음)** — 오프라인 설치 묶음 `minedocscan-1.0.0-win64.zip` 하나로 설치합니다. 앱 전용 파이썬과 필요한 꾸러미가
  들어 있어 망에 닿지 않고, 관리자 권한 없이 `install.ps1` 이 지금 사용자로 설치하며, 작업 스케줄러가 로그온할 때 화면(`serve`)을 띄웁니다
  (CI 의 윈도우 러너에서 묶음 76 MB, 망을 막고 설치 15초). 순서·매개변수·올리기·지우기는 설명서 [2장 설치](docs/manual/2-설치.md),
  묶음을 만드는 도구는 [scripts/](scripts/README.md).
- **리눅스** — 묶음이 없습니다. 파이썬 3.11 또는 3.12 에서 `pip install "./mine-docscan[postgres]"` (통합 DB 에 싣지 않으면 `[postgres]` 없이 —
  [2장의 "리눅스"](docs/manual/2-설치.md#리눅스)).

설치한 뒤에는 **자가 시험**으로 그 PC 에서 제대로 도는지 봅니다. 합성 데이터만 임시 폴더에서 돌리고 이 PC 의 설정·데이터 폴더에는 닿지 않습니다
([3장 "자가 시험"](docs/manual/3-처음-설정.md#자가-시험)).

```bash
minedocscan --version                 # minedocscan 1.0.0
minedocscan selftest --out <폴더>     # 접수 → 처리 → 검수·다시 처리 → 엑셀·가린 그림 → 화면. 통과 0 / 실패 1 (CI: 윈도우 49초, 우분투 약 27초)
                                      # 결과 selftest.md·selftest.json — 경로·이름 없이. --publish-schema 면 통합 DB 까지
```

## 빨리 돌려 보기

실제 현장 데이터 없이, 생성한 합성 양식으로 전 과정을 확인할 수 있습니다.

```bash
git clone https://github.com/junhpark/mine-docscan.git && cd mine-docscan
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                                                    # 약 7–8분 (실데이터·torch 불필요)

minedocscan synth out/demo                                # 합성 사이트 팩 + 3일치 스캔 + 정답
minedocscan run   --site out/demo/site --archive-root out/demo/scans --work-root out/demo/work
minedocscan eval  --answers out/demo/answers.json --work-root out/demo/work
minedocscan review serve --site out/demo/site --work-root out/demo/work --reviewer me   # 검수 화면 (127.0.0.1:8765)
```

`run` 이 끝나면 양식별 페이지 수, 정합 품질, 검수 대기 필드 수, 교차검증 결과가 나옵니다.
인식 백엔드가 없으면(`null`) 손글씨 값은 전부 검수 대기로 가고, 정답을 돌려주는 시험용 백엔드
(`run --answers out/demo/answers.json`)로 돌리면 오류가 0 이어야 합니다.

## 실제 데이터로 돌리기

스캔 원본과 사이트 팩은 저장소 밖에 있습니다 ([docs/DATA.md](docs/DATA.md)).

```bash
export MINEDOCSCAN_SITE=/경로/site-packs/<현장>          # 템플릿·현장 옵션·페이지 라벨. 엑셀·통합 DB 에는 site.toml 의 [site] name 이
                                                          # 있어야 한다 (사본의 주인 — docs/SITE_PACK.md)
export MINEDOCSCAN_ARCHIVE_ROOT=/경로/mine-docscan        # 스캔 원본 (접수한 파일을 intake/ 아래에만 쓴다 — 그 밖은 읽기만)
export MINEDOCSCAN_INBOX=/경로/스캐너-저장-폴더          # 접수 폴더 (선택 — watch·serve 가 본다)
export MINEDOCSCAN_WORK_ROOT=/로컬/작업폴더               # DB·정합 이미지 (로컬 디스크)
export MINEDOCSCAN_EXCEL_DIR=/경로/엑셀-폴더              # 엑셀 사본 (선택 — 있는 폴더, 저장소·접수 폴더·보관 폴더 밖)
export MINEDOCSCAN_PUBLISH_URL=postgresql://계정@호스트/DB   # 통합 DB (선택 — 환경변수로만, 설정 파일에 적지 않는다. 비밀번호는 URL 이
                                                          # 아니라 libpq 의 pgpass 파일에 — docs/DATA.md "통합 DB")

minedocscan info                    # 설정과 템플릿 확인 (통합 DB 는 호스트·DB 이름만)
minedocscan run DB_scans            # 폴더 또는 파일. 같은 파일을 다시 넣어도 행이 늘지 않습니다
minedocscan report
minedocscan regress                 # 사이트 팩에 저장한 기준 수치와 비교

minedocscan serve --reviewer me     # 접수 폴더 감시 + 운영 화면 (127.0.0.1:8765): 날짜를 정할 문서, 다시 스캔 의심 쪽, 대기열
                                    # 바퀴 끝(접수 → 처리 → 엑셀 → 싣기)에, 설정이 있으면(excel_dir·MINEDOCSCAN_PUBLISH_URL) 바뀐 날짜의
                                    # 엑셀을 다시 쓰고 바뀐 문서·날짜를 통합 DB 에 싣는다 (시작할 때와 sweep_minutes 마다 전체 훑기,
                                    # --no-watch 면 하지 않는다). 홈에서 날짜·달의 엑셀 내려받기 (설정 없이도)
minedocscan doc list                # 문서 목록 (날짜를 정하기·버리기는 doc date|discard 또는 화면에서)

minedocscan export excel [--month 2030-01]   # 일별·월별 엑셀을 손으로 (바뀐 파일만). serve·watch 가 돌면 그쪽이 쓴다
minedocscan publish [--check]       # 통합 DB(PostgreSQL)로 싣기 — 지문이 다른 문서·날짜만 한 트랜잭션으로. serve·watch 가 돌면 그쪽이 싣는다
                                    # --check 는 쓰지 않고 다른 범위의 수만 (같으면 0, 다르면 1, 닿지 못함·설정 오류·다른 사이트의 대상 2).
                                    # pip install -e ".[postgres]"
minedocscan export masked-pages /경로/밖 --date 2030-01-07   # 서명·작성자·차량번호 필드, 템플릿의 redact 상자, 글자 칸을
                                                             # 한 색으로 가린 쪽 그림 (적재된 쪽만, 발표·보고서용)
```

합성 접수 폴더로 해 보려면 `minedocscan synth out/intake --intake` (돌아간 쪽, 날짜 없는 이름, 빈 뒷면, 다시 스캔, 잘린 PDF …).

엑셀과 통합 DB 의 표는 작업 DB 의 **사본**입니다 — 고쳐도 작업 DB 로 돌아오지 않습니다(고치는 곳은 검수 화면).
엑셀에는 확정된 값만 싣습니다 (검수 대기 칸은 값 없이 `?`, 사람이 읽지 못한 칸은 `판독 불가`; 기계 값은 `[export] machine_values` 일 때만 따로 둔 열에).
통합 DB 에는 작업 DB 의 행이 그대로 갑니다 — 검수 대기 행과 기계 값도 `review_status` 와 함께 가므로 읽는 쪽이 `review_status` 로 거릅니다.
통합 DB 의 이 표들에는 쓰지 않습니다 — 다음 싣기가 범위째 갈아 끼웁니다. 2단계의 입력은 2단계의 표에 적고 뷰로 합칩니다.
작업 DB 는 로컬 SQLite 그대로이고, 엑셀 파일이 열려 있거나 통합 DB 가 꺼져 있어도 스캔과 검수는 멈추지 않습니다
([ADR 0021](docs/decisions/0021-publish-to-the-shared-db.md), [ADR 0022](docs/decisions/0022-exports-are-copies.md)).
가린 쪽 그림은 템플릿이 아는 자리만 가립니다 — 사람이 보고 나서 씁니다.

환경변수 대신 `minedocscan.toml` 을 써도 됩니다 ([config/minedocscan.example.toml](config/minedocscan.example.toml)).

## 저장소 구성

```
src/minedocscan/
  imaging/     영상처리: 입출력, 괘선 검출, 정합, 셀·체크·글씨 덩어리
  forms/       템플릿, 사이트 팩, 양식 분류
  recognize/   인식 백엔드 (인터페이스 + null, oracle, digits — 숫자 칸) · 표 밖 필드의 모델 (meta, choice)
  correct/     교정 백엔드 (인터페이스 + none)
  handlers/    양식의 의미: 셀 → 업무 테이블 (generic, inspection, haul, usage)
  validate/    양식 간 교차검증, 가동 일보의 계기 검산
  store/       스키마와 쓰기 도우미, 문서의 순서
  intake/      접수 폴더, 결정 기록, 사람이 넣는 날짜, 감시 바퀴
  pipeline/    실행기, 한 번에 하나만 도는 잠금
  export/      내보내기: 일별·월별 엑셀, 가린 쪽 그림 (DB 의 사본 — 읽기만 한다)
  publish/     통합 DB(PostgreSQL)로 싣기 (문서·날짜 단위 지문)
  evaluate/    지표, 정답 비교, 실데이터 회귀
  tools/       합성 데이터 생성기(가상 양식 V2 포함), 템플릿 도구
  selftest.py  자가 시험 (minedocscan selftest)
  cli.py       minedocscan 명령
tests/         합성 데이터 기반 시험 (+ realdata/ 는 실데이터가 있을 때만)
docs/          구조, 사이트 팩 형식, 데이터 관리, 로드맵, 결정 기록, 사용 설명서(manual/), 시험 성적서(test-report/)
scripts/       설치 묶음·설치 스크립트·라이선스 목록·설명서·성적서·규모를 만드는 도구 (패키지에 들지 않는다)
config/        설정 예시
```

## 문서

| 문서 | 내용 |
|---|---|
| [docs/manual/](docs/manual/README.md) | 사용 설명서 — 설치, 처음 설정, 날마다 쓰기, 관리, 문제 해결, 새 양식 더하기, 명령·설정 목록 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 구조, 단계별 설계, 데이터 모델, 확장 지점 |
| [docs/SITE_PACK.md](docs/SITE_PACK.md) | 사이트 팩과 템플릿 형식, 새 양식 추가 절차 |
| [docs/DATA.md](docs/DATA.md) | 데이터 위치, 커밋 금지 대상, 평가·회귀 방법 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 현재 상태, 다음 단계, 결정이 필요한 사항 |
| [docs/PRIOR_WORK.md](docs/PRIOR_WORK.md) | 선행 연구에서 이어받은 요구사항과 바꾼 것 |
| [docs/decisions/](docs/decisions/) | 설계 결정 기록 |
| [docs/test-report/](docs/test-report/README.md) | 시험 성적서의 구성과 만드는 법 (성적서는 CI 산출물) |
| [CHANGELOG.md](CHANGELOG.md) | 판마다 바뀐 것 |
| [docs/tasks/](docs/tasks/) | 작업 지시서 (Claude Code 에 맡기는 단위) |
| [CLAUDE.md](CLAUDE.md) | Claude Code 로 작업할 때의 규칙과 요약 |

## 현재 상태

판 1.0.0. 1단계(스캐너–인식–데이터베이스)가 접수 폴더에서 엑셀·통합 DB 까지 동작하고, 윈도우 현장 PC 에 오프라인 묶음으로 설치합니다
(설치는 CI 의 윈도우 러너에서 시험했고, 현장 PC 에서는 아직 해 보지 않았습니다).
셀을 보고 값을 넣는 검수 화면이 있습니다 (검수값이 곧 정답·학습 데이터가 됩니다). 숫자 칸·표 밖 필드의 인식기는 현장 글씨로 학습한
모델이 있어야 쓰이고, 글자 칸을 읽는 인식 백엔드는 아직 없습니다. 실데이터의 인식 수치도 아직 없습니다.
무엇이 구현되었고 무엇이 남았는지는 [docs/ROADMAP.md](docs/ROADMAP.md) 에 있습니다.

## 주의

현장 문서에는 개인정보(이름·서명·차량번호)가 있습니다. 스캔 원본, 기준 이미지, 실제 템플릿, 라벨, 정답 파일을
이 저장소에 커밋하지 마십시오. `.gitignore` 가 이미지·PDF·엑셀·CSV·DB 를 기본으로 막고 있습니다.
내보낸 엑셀과 가린 쪽 그림도 현장 데이터입니다 (명령이 저장소 안을 거절합니다). 통합 DB 의 주소는 환경변수로만, 비밀번호는 pgpass 파일에 둡니다.
