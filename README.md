# mine-docscan

광산 현장의 수기 문서(일일 점검표, 작업일보 등)를 스캔하면 양식을 알아보고, 칸마다 값을 뽑아,
출처와 함께 데이터베이스에 적재하는 파이프라인입니다. 패키지와 명령 이름은 `minedocscan` 입니다.

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

## 빨리 돌려 보기

실제 현장 데이터 없이, 생성한 합성 양식으로 전 과정을 확인할 수 있습니다.

```bash
git clone https://github.com/junhpark/mine-docscan.git && cd mine-docscan
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                                                    # 약 1분

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
export MINEDOCSCAN_SITE=/경로/site-packs/<현장>          # 템플릿·현장 옵션·페이지 라벨
export MINEDOCSCAN_ARCHIVE_ROOT=/경로/mine-docscan        # 스캔 원본 (읽기 전용으로 취급)
export MINEDOCSCAN_WORK_ROOT=/로컬/작업폴더               # DB·정합 이미지 (로컬 디스크)

minedocscan info                    # 설정과 템플릿 확인
minedocscan run DB_scans            # 폴더 또는 파일. 같은 파일을 다시 넣어도 행이 늘지 않습니다
minedocscan report
minedocscan regress                 # 사이트 팩에 저장한 기준 수치와 비교
```

환경변수 대신 `minedocscan.toml` 을 써도 됩니다 ([config/minedocscan.example.toml](config/minedocscan.example.toml)).

## 저장소 구성

```
src/minedocscan/
  imaging/     영상처리: 입출력, 괘선 검출, 정합, 셀·체크·글씨 덩어리
  forms/       템플릿, 사이트 팩, 양식 분류
  recognize/   인식 백엔드 (인터페이스 + null, oracle)
  correct/     교정 백엔드 (인터페이스 + none)
  handlers/    양식의 의미: 셀 → 업무 테이블 (generic, inspection, haul, usage)
  validate/    양식 간 교차검증, 가동 일보의 계기 검산
  store/       스키마와 쓰기 도우미
  pipeline/    실행기
  evaluate/    지표, 정답 비교, 실데이터 회귀
  tools/       합성 데이터 생성기, 템플릿 뼈대 도구
  cli.py       minedocscan 명령
tests/         합성 데이터 기반 시험 (+ realdata/ 는 실데이터가 있을 때만)
docs/          구조, 사이트 팩 형식, 데이터 관리, 로드맵, 결정 기록
config/        설정 예시
```

## 문서

| 문서 | 내용 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 구조, 단계별 설계, 데이터 모델, 확장 지점 |
| [docs/SITE_PACK.md](docs/SITE_PACK.md) | 사이트 팩과 템플릿 형식, 새 양식 추가 절차 |
| [docs/DATA.md](docs/DATA.md) | 데이터 위치, 커밋 금지 대상, 평가·회귀 방법 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 현재 상태, 다음 단계, 결정이 필요한 사항 |
| [docs/PRIOR_WORK.md](docs/PRIOR_WORK.md) | 선행 연구에서 이어받은 요구사항과 바꾼 것 |
| [docs/decisions/](docs/decisions/) | 설계 결정 기록 |
| [docs/tasks/](docs/tasks/) | 작업 지시서 (Claude Code 에 맡기는 단위) |
| [CLAUDE.md](CLAUDE.md) | Claude Code 로 작업할 때의 규칙과 요약 |

## 현재 상태

1단계(스캐너–인식–데이터베이스)의 골격이 동작하고, 셀을 보고 값을 입력하는 최소 검수 도구가 있습니다
(검수값이 곧 정답·학습 데이터가 됩니다). 손글씨를 읽는 인식 백엔드는 아직 없습니다.
무엇이 구현되었고 무엇이 남았는지는 [docs/ROADMAP.md](docs/ROADMAP.md) 에 있습니다.

## 주의

현장 문서에는 개인정보(이름·서명·차량번호)가 있습니다. 스캔 원본, 기준 이미지, 실제 템플릿, 라벨, 정답 파일을
이 저장소에 커밋하지 마십시오. `.gitignore` 가 이미지·PDF·엑셀·CSV·DB 를 기본으로 막고 있습니다.
