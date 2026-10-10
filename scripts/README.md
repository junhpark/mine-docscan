# scripts/ — 묶음을 만들고 재는 도구

설치되는 패키지(`src/minedocscan`)에 들어가지 않는다. 저장소에서 개발자·CI 가 돌린다. 현장 데이터를 읽지 않는다 — 합성 데이터만 만들고 잰다
(`bigdb.py measure` 도 합성 복제 DB 를 잰다).

| 파일 | 하는 일 | 쓰는 곳 |
|---|---|---|
| `bundle.py` + `bundle.toml` | 윈도우 오프라인 설치 묶음 `minedocscan-<판>-win64.zip` — embeddable 파이썬·pip 바퀴(해시 고정), `wheels/`, 설치 스크립트, 라이선스 목록, `manual.html`, `SHA256SUMS.txt`. 옆에 `bundle-report.json` (크기·바퀴·VC 런타임) | CI `windows-install` (tasks/0009 4.5) |
| `windows/install.ps1`, `uninstall.ps1`, `INSTALL.txt` | 묶음에 들어가는 설치·지우기 (Windows PowerShell 5.1, 관리자 권한 없이, 망에 닿지 않고). 묶음을 만들 때 UTF-8 BOM·CRLF 로 | 묶음 |
| `licenses.py` + `licenses/` | 제3자 라이선스 목록 `THIRD_PARTY_NOTICES.txt` 와 허용 목록 검사(`--check`). `licenses/` 는 바퀴 밖에서 같이 드는 것(파이썬·pip·바퀴에 든 C 라이브러리)의 본문 | CI `test`, `bundle.py` (tasks/0009 4.3) |
| `cli_reference.py` | 설명서의 부록 `docs/manual/명령.md` 를 argparse 의 동작에서 만든다 (`--check` — 시험이 본다) | 명령을 바꾼 뒤 |
| `manual_shots.py` | 설명서의 갈무리 `docs/manual/img/` — 합성 접수 폴더를 처리하고 운영 화면을 Playwright 로 (`pip install -e ".[docs]"`) | 화면을 바꾼 뒤 |
| `manual_html.py` | 설명서 → 그림을 담은 한 장짜리 `manual.html` (`.[docs]` 의 markdown) | `bundle.py`, CI |
| `test_report.py` | 시험 성적서 `시험성적서-<판>.md` — CI 산출물(JUnit·자가 시험·설치 단계·확장성·라이선스)과 `docs/test-report/scale-*.json`. `env` 는 작업마다의 환경 | CI `report` (tasks/0009 4.9) |
| `v2_metrics.py` | 확장성 표 — 가상 양식(V2)의 쪽·분류·정합·칸·oracle CER·값 유무, 보통·거친 글씨 | CI `slow` (tasks/0009 4.7) |
| `bigdb.py` | 규모 — 합성 묶음을 한 해 규모로 복제한 작업 DB 와 엑셀·싣기의 시간·메모리 → `docs/test-report/scale-*.json` | 손으로 (tasks/0009 4.2 마) |

만드는 법은 각 파일의 첫 독스트링에. 무엇을 어디에 커밋하는지: 성적서의 틀·생성기·규모의 JSON 은 저장소에, 성적서·묶음·`manual.html` 은 CI 산출물
(`docs/test-report/README.md`).
