# 0024. 설치는 오프라인 묶음(zip + PowerShell)과 작업 스케줄러 — 앱 전용 파이썬, 지금 사용자로, 관리자 권한 없이

상태: 채택 (2026-10). 작업 지시서 [tasks/0009](../tasks/0009-release.md) 1절 가, 4.5, 9절(설치의 모양·파이썬·`serve` 를 띄우는 것·설치 위치와
권한·통합 DB 의 비밀번호·리눅스 설치). 설치의 순서는 [사용 설명서 2장](../manual/2-설치.md), 설치가 남기고 지우는 것은 [DATA.md](../DATA.md)
"현장 PC: 설치 폴더와 데이터 폴더", 묶음과 자가 시험의 구조는 [ARCHITECTURE.md](../ARCHITECTURE.md) §11.1.

## 상황

- 1단계 소프트웨어는 독립 소프트웨어로 등록한다 — 혼자서 설치·실행·시험이 되어야 한다. 지금까지는 개발자의 리눅스에서 `pip install -e` 로만 돌았다.
- 현장 PC 는 윈도우이고 외부망이 없다 (ADR 0003). `pip install` 이 PyPI 에 닿지 못한다 — 파이썬과 바퀴(wheel)를 다 담은 묶음이 있어야 한다.
- `serve`(접수 폴더 감시 + 운영 화면)는 켤 때마다 사람이 명령을 쳐야 했다. 로그온하면 떠 있어야 한다.
- `serve` 는 그 사용자의 자리를 쓴다: 스캐너의 저장 폴더(접수), 현장이 여는 엑셀 폴더(네트워크 폴더일 수 있다), 사용자 환경 변수의
  통합 DB 주소(ADR 0021 — 환경 변수로만 받는다).
- 현장 PC 에 관리자 권한이 있는지, 다른 파이썬이 깔려 있는지 모른다.
- 윈도우 PC 는 작업 환경에 없다 — 윈도우에서 해야 하는 것은 CI 의 윈도우 러너(`windows-latest`)에서 한다.

## 결정

1. **설치의 모양은 zip 하나 + PowerShell 스크립트.** 설치 프로그램(.exe·MSI)·코드 서명·자동 업데이트는 하지 않는다. 묶음
   `minedocscan-<판>-win64.zip` 은 `scripts/bundle.py` 가 인터넷이 되는 곳(CI 의 `windows-install` 작업)에서 만든다 — 앱 전용 파이썬과 pip 바퀴,
   `wheels/`(minedocscan 과 런타임 의존성 전부, `[postgres]` 포함, `cp312-win_amd64`), `install.ps1`·`uninstall.ps1`·`INSTALL.txt`(UTF-8 BOM·CRLF —
   Windows PowerShell 5.1 은 BOM 이 없으면 한글을 깨뜨린다), `THIRD_PARTY_NOTICES.txt`(라이선스 검사 — ADR 0023)·`NOTICE`·`manual.html`·설정의 보기·
   `VERSION`·`SHA256SUMS.txt`. zip 안의 이름은 ASCII. 묶음을 만드는 도구는 `scripts/` 에 두어 설치되는 패키지에 들지 않는다.
2. **파이썬은 앱 전용 embeddable 3.12** (python.org 의 embeddable 패키지 3.12.10). 판과 SHA-256 을 `scripts/bundle.toml` 에 적어 두고(pip 바퀴도)
   받은 것이 다르면 묶음 만들기가 멈춘다. 레지스트리·시작 메뉴를 건드리지 않고 PC 의 다른 파이썬과 섞이지 않는다. 지원하는 파이썬은
   3.11–3.12 그대로다 (CI 의 `test` 가 둘 다 돈다).
3. **지금 사용자로, 관리자 권한 없이, 망에 닿지 않고.** 프로그램은 `%LOCALAPPDATA%\minedocscan`(`-InstallDir`), 설정은
   `%APPDATA%\minedocscan\minedocscan.toml`(없을 때만 쓴다 — 있으면 건드리지 않는다), 사용자 환경 변수 `MINEDOCSCAN_CONFIG`, 사용자 PATH 에
   `python\Scripts`. pip 은 `--isolated --no-index --find-links wheels` (사용자의 pip 설정·`PIP_*` 가 섞이지 않게).
4. **`serve` 는 작업 스케줄러가 띄운다 — 윈도우 서비스가 아니다.** 작업 `minedocscan-serve`: 그 사용자가 로그온할 때, 그 사용자로
   (`LogonType Interactive`, `RunLevel Limited` — 작업에 비밀번호를 맡기지 않는다), `pythonw -m minedocscan.cli serve --config … --reviewer …
   --log-dir <설치 폴더>\logs`(창 없이), 실패하면 1분 뒤 다시(세 번), 시간 제한 없음, 배터리에서도 시작하고 멈추지 않는다. `pythonw` 에는 표준
   출력이 없으므로 `serve --log-dir` 이 날마다 UTF-8 로그를 쓴다 (`logfile.py`, 인자를 읽기 전에 연다). 바탕 화면에 바로 가기 "광산 문서 스캔"
   (`http://127.0.0.1:8765/`).
5. **통합 DB 의 비밀번호는 libpq 의 pgpass.** 설치는 통합 DB 의 URL 을 받지 않는다. URL(`MINEDOCSCAN_PUBLISH_URL=postgresql://계정@서버/DB`)에는
   비밀번호를 넣지 않고 `%APPDATA%\postgresql\pgpass.conf` 에 둔다. 프로그램은 바꾸지 않는다 — 연결할 때 libpq 가 읽는다
   (시험: `-m postgres` 의 `test_the_password_comes_from_pgpass`).
6. **설치의 순서** (`install.ps1`) — 바꾸기 전에 멈출 수 있는 것은 다 멈춘다:
   - 묶음을 `SHA256SUMS.txt` 로 확인한다 — 해시가 다르거나, 설치하는 파일(바퀴·파이썬·pip·스크립트)이 목록에 없거나, 목록이 비었으면 멈춘다.
     받은 zip 의 차단 표시(Mark of the Web)가 남았으면 푼다. 매개변수(절대 경로, 검수자 ID)를 본다.
   - `python.new` 에 embeddable 을 풀고 `._pth` 의 `import site` 를 켠다 → pip 바퀴를 `site-packages` 에 풀고 그 pip 으로 자기를 다시 설치한다 →
     `minedocscan[postgres]` → **런타임 확인**(`cv2`·`numpy`·`pypdfium2`·`psycopg`·`openpyxl` 을 읽어 들인다 — 실패하면 Visual C++ 재배포 패키지와
     N·KN 판의 미디어 기능 팩을 안내).
   - 그 뒤에야 작업을 멈추고 `python` 과 바꿔 끼운다 (다시 시도하고, 실패하면 되돌린다). 명령 실행 파일(`minedocscan.exe`·`pip.exe` — 파이썬의
     절대 경로를 담는다)을 제자리의 파이썬으로 다시 만든다.
   - 설정·환경 변수·PATH → `minedocscan info` → 작업 스케줄러 → 바로 가기 → 요약 (다음에 할 일: `minedocscan selftest`, 설명서 3장).
7. **올리기 = 새 판의 묶음으로 `install.ps1` 을 다시.** 옆에 만들어 바꿔 끼우므로 어디서 실패해도 옛 판이 남는다. 설정·로그는 그대로 —
   `-Config` 가 없으면 `MINEDOCSCAN_CONFIG` 의 설정을 그대로 쓴다. 작업 DB 의 스키마 버전이 바뀌는 판이면 `run --fresh` 를 안내만 한다 (스스로
   돌리지 않는다).
8. **지우기(`uninstall.ps1`)는 프로그램만**: 작업·바로 가기·`python\`·`logs\`·`VERSION`·PATH 항목·`MINEDOCSCAN_CONFIG`. 설정 파일과 사이트 팩·보관·작업·
   접수·엑셀 폴더는 건드리지 않고 자리를 찍는다 (`-RemoveConfig` 일 때만 `%APPDATA%` 의 설정도). minedocscan 의 설치 폴더(`VERSION` 이나 앱 전용
   파이썬 안의 minedocscan)가 아니면 아무것도 지우지 않는다.
9. **리눅스는 `pip`** (설명서 2장의 한 절). 맥·리눅스 묶음은 만들지 않는다.
10. 설치한 뒤의 확인은 `minedocscan selftest` — 설치한 프로그램만으로, 합성 데이터만으로, 망 없이, 3분 안 (ARCHITECTURE §11.1).

## 이유

- **zip + PowerShell**: Windows PowerShell 5.1 은 윈도우에 들어 있어 현장 PC 에 더 깔 것이 없다. 스크립트는 글이라 현장의 담당자가 읽어 볼 수
  있고, 묶음은 CI 의 윈도우 러너에서 `scripts/bundle.py` 하나로 만든다 (설치 프로그램을 만드는 도구가 더 들지 않는다). 코드 서명에는 기관이 가진
  인증서가 있어야 한다 — 등록·배포의 형태는 사람이 정한다 (tasks/0009 8절).
- **앱 전용 embeddable**: python.org 의 설치 파일은 지금 사용자로 설치해도 레지스트리·시작 메뉴·"앱 및 기능"에 남는다. embeddable 은 폴더
  하나라 지우면 끝이고, 옆에 만들어 이름만 바꾸는 올리기가 된다. 지시서는 "embeddable 로 되지 않는 것이 있으면 설치 파일로 바꾼다"고 했다 —
  되지 않는 것은 없었다.
- **3.12.10**: 지원하는 판(3.11–3.12) 중 새 것이고, 3.12 의 마지막 바이너리 판이다 (그 뒤의 3.12 는 소스만 낸다 — 3.12.11 부터는 embeddable 이 없다).
- **관리자 권한 없이**: 현장 PC 의 관리자 권한을 설치의 조건으로 두지 않는다. `%LOCALAPPDATA%`, 사용자 PATH·사용자 환경 변수, 자기 계정의
  작업 스케줄러 작업은 관리자 권한 없이 된다.
- **작업 스케줄러, 서비스가 아니다**: 서비스는 설치에 관리자 권한이 들고, 그 사용자의 로그온 세션 밖에서 서비스 계정(또는 비밀번호를 맡긴 계정)으로
  돈다 — 사용자가 연결한 드라이브 문자·네트워크 공유의 자격(엑셀 폴더가 그런 자리일 수 있다), 사용자 환경 변수(`MINEDOCSCAN_CONFIG`·
  `MINEDOCSCAN_PUBLISH_URL`), 사용자의 `pgpass.conf`(`%APPDATA%`)가 다 그 사용자의 것이다. 로그온할 때 그 사용자로 띄우면 셋을 그대로 쓴다.
  파이썬을 서비스로 돌리려면 감싸는 프로그램이 더 든다.
- **pgpass**: 환경 변수는 그 사용자로 도는 프로그램에 다 넘어가므로 비밀번호를 거기 두지 않는다 ([DATA.md](../DATA.md) "통합 DB"). pgpass 는 libpq 가
  연결할 때 읽는 자리라 프로그램에 바꿀 것이 없고, 설치 스크립트·로그·자가 시험의 결과에 비밀값이 지나갈 길이 없다 (tasks/0009 7절).
- **옆에 만들어 바꿔 끼운다**: 설치가 중간에 끊기거나 런타임 확인이 실패해도 돌던 판이 남는다. 현장에는 설치를 되돌려 줄 사람이 없다.

## 버린 것

- **설치 프로그램(.exe·MSI)·코드 서명** — 위의 이유. 기관이 서명·배포의 방식을 정하면 다시 본다.
- **윈도우 서비스** — 위의 이유.
- **python.org 의 설치 파일**(지금 사용자, 런처 없이) — embeddable 로 되지 않는 것이 있을 때의 대안으로 두었고 쓰지 않았다.
- **PC 에 있는 파이썬에 설치하기** — 있는지, 어느 판인지 모르고, 다른 프로그램의 꾸러미와 섞인다. 망이 없어 판이 맞지 않으면 고칠 길이 없다.
- **바퀴 안에서 pip 을 돌리기**(`python pip-….whl/pip install …`) — pip 이 지원하는 길이 아니고, embeddable 은 `._pth` 가 `sys.path` 를 정한다.
  바퀴를 `site-packages` 에 풀고 그 pip 으로 자기를 다시 설치한다 (`RECORD`·`Scripts` 가 생긴다).
- **제자리에 덮어 설치하기** — 실패하면 반쯤 바뀐 파이썬이 남는다.
- **설치가 통합 DB 의 URL·비밀번호를 받기** — 스크립트의 인자·화면·로그에 남는다.
- **`python.exe` 로 띄우기** — 로그온할 때마다 창이 뜨고, 그 창을 닫으면 `serve` 가 끝난다.
- **자동 업데이트** — 망이 없다. 올리기는 새 묶음으로 `install.ps1` 을 다시 돌리는 것이다.

## CI 에서 확인한 것 (tasks/0009 단계 5, 윈도우 러너)

`windows-install` 작업 (`shell: powershell` — 현장과 같은 Windows PowerShell 5.1): 묶음 만들기 → **망을 막고**(`PIP_NO_INDEX=1`, 닿지 않는 프록시)
설치 → 새 창처럼(레지스트리의 사용자 PATH·`MINEDOCSCAN_CONFIG`) `minedocscan --version`·설치한 `minedocscan selftest` → 등록된 작업의 정의(동작·로그온
트리거·다시 시도·배터리·시간 제한)와 그 작업이 띄운 `serve` 의 `/api/home`·로그 → 같은 묶음으로 다시 설치 → 파일 하나를 바꾼 묶음 → 지우기.
첫 실행에 통과했다.

| 확인 | 결과 |
|---|---|
| 묶음 | 76.0 MB (79,728,580 바이트), 파일 23개, 바퀴 12개 — 파이썬 3.12.10, pip 26.2.1. 만들기 17초 |
| 설치 (망을 막고) | 15초 |
| 같은 묶음으로 다시 설치 (올리기의 길) | 14초. 설정 파일의 해시가 그대로, 바꿔 끼운 뒤의 `minedocscan.exe` 가 돈다 |
| 설치한 자가 시험 | 윈도우 49.4초 (우분투 약 27초) — 기준 3분 |
| `serve` | 작업 스케줄러의 작업으로 떴다 (같은 명령줄을 직접 띄우는 대신은 쓰지 않았다). `/api/home` 200, 로그가 UTF-8 |
| 바퀴 하나를 바꾼 묶음 | 멈췄다 — 파이썬을 풀지 않았다 |
| 지우기 | 작업·설치 폴더·PATH 항목·`MINEDOCSCAN_CONFIG` 가 없고, 설정 파일과 데이터 폴더 다섯은 남았다 |
| `msvcp140.dll` | PC 에 없어도 된다 — numpy·rapidfuzz 는 자기 것을 담고, OpenCV·PDFium 의 바이너리는 가져오지 않으며, `vcruntime140(_1)` 은 embeddable 이 준다 (`bundle-report.json` — 바이너리의 가져오기 표) |

윈도우의 기본 `pytest` 는 `windows` 작업이 돈다 (545 통과·21 건너뜀, 718초 — 단계 4). 윈도우에서 처음 돌려 고친 것은 지시서 0009 12절.

## 대가

- **현장 PC 에서 해 본 것이 아니다** — CI 의 러너뿐이다. 러너에는 Visual C++ 재배포 패키지가 있어, 그것이 없는 PC 는 CI 로 잡지 못한다 (가져오기
  표로는 필요 없다). N·KN 판 윈도우의 미디어 기능 팩도 같다. "관리자 권한 없이"도 러너의 계정으로 돌렸을 뿐 권한이 없는 계정으로 설치해 본
  것이 아니다. 현장 PC 에서의 설치는 사람의 일이다 (tasks/0009 8절 2).
- 서명하지 않은 스크립트다: 받은 zip 의 차단을 풀고(`install.ps1` 도 묶음 폴더 안의 표시를 푼다) 그 한 번만 `-ExecutionPolicy Bypass` 로 돌린다
  (PC 의 정책은 그대로). `SHA256SUMS.txt` 는 묶음 안의 목록이라 손상·바뀐 파일은 잡지만 목록까지 같이 바꾼 묶음은 잡지 못한다 — 묶음은 믿을
  수 있는 자리에서 받는다.
- **`serve` 는 그 사용자가 로그온해 있을 때만 돈다.** PC 를 켜 두고 아무도 로그온하지 않으면 접수하지 않는다 (스캔은 접수 폴더에 남았다가 로그온하면
  들어간다). 로그오프하면 멈춘다.
- 사용자마다 따로 설치된다 — 다른 계정으로 쓰려면 그 계정으로 다시 설치한다.
- 앱 전용 파이썬은 PC 의 파이썬과 따로 올려야 한다 — 파이썬의 수정은 새 묶음으로만 들어간다. 3.12 의 바이너리는 3.12.10 이 마지막이라 그 뒤의
  수정은 판을 올려야 받는다 (그때 CI 의 판과 `scripts/bundle.toml` 을 같이).
- Windows PowerShell 5.1 의 함정을 스크립트가 진다: 스크립트는 BOM 이 있어야 한글을 읽고, 바깥 프로그램이 표준 오류에 쓴 줄이 멈춤이 될 수
  있고, 인자의 큰따옴표가 지워지고, 바깥 프로그램의 출력은 UTF-8 로 읽어야 한다. CI 의 `run` 블록은 ASCII 로만 쓴다 (러너가 BOM 없는 임시
  `.ps1` 로 돌려 5.1 이 cp1252 로 읽는다).
