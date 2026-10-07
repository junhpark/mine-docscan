# mine-docscan — 작업 안내 (Claude Code 용)

광산 현장의 수기 문서를 스캔하면 바로 데이터베이스에 들어가게 하는 파이프라인이다.
패키지·CLI 이름은 `minedocscan`. 이 파일은 저장소에서 작업할 때 먼저 읽는 요약이고,
자세한 내용은 `docs/` 에 있다.

- `docs/ARCHITECTURE.md` — 구조, 데이터 모델, 확장 지점
- `docs/SITE_PACK.md` — 양식 정의(템플릿) 형식, 새 양식 추가 절차
- `docs/DATA.md` — 데이터가 어디에 있고 무엇을 커밋하면 안 되는지
- `docs/ROADMAP.md` — 구현된 것 / 남은 것 / 결정이 필요한 것
- `docs/PRIOR_WORK.md` — 선행 연구에서 이어받은 요구사항과 바꾼 것
- `docs/decisions/` — 왜 이렇게 했는지 (ADR)
- `docs/tasks/` — 작업 지시서. 맡은 작업의 범위·설계·수용 기준은 여기서 읽는다

## 범위

이 저장소는 **1단계 소프트웨어**다: 스캐너 → 인식 → 데이터베이스. 독립 소프트웨어로 등록할 예정이므로
혼자서 설치·실행·시험이 되어야 한다. 2단계(통합 DB, 입력 체계, 시각화)는 이 저장소의 업무 테이블
(`insp_*`, `prod_*`, `xcheck_*`, `eq_*`)을 읽어 가는 별도 작업이다. 2단계 기능을 여기에 넣지 않는다.

물질수지 관점: 현장 문서의 값은 서로 맞지 않는다. 1단계는 그 차이를 **고치지 않고 계산해서 보여 준다**
(`xcheck_*`). 조정(reconciliation)은 2단계의 일이다.

## 자주 쓰는 명령

```bash
pip install -e ".[dev]"

pytest                      # 합성 양식으로 전체 시험 (약 6–7분, 실데이터·torch 불필요)
pytest -m slow              # 무거운 시험 (다시 처리의 불변식 흔들기, 합성 접수 시나리오, 돌린 쪽의 비교 — CI 의 slow 작업)
pytest -m train             # 숫자 인식기 학습 시험 (torch 필요: pip install -e ".[train]", 몇 분)
ruff check .

minedocscan synth out/demo                  # 개인정보 없는 합성 사이트 팩 + 스캔 + 정답
minedocscan run   --site out/demo/site --archive-root out/demo/scans --work-root out/demo/work
minedocscan report --work-root out/demo/work
minedocscan eval  --answers out/demo/answers.json --work-root out/demo/work

minedocscan review serve --site out/demo/site --work-root out/demo/work --queue haul-numbers --reviewer jp
                                            # 127.0.0.1:8765 — 셀을 보고 값을 입력. 기록은 <site>/reviews/reviews.jsonl
                                            # --queue page-fields 는 일보의 차량번호·작성자 (수동 라벨 대신)
minedocscan review stats  --site … --work-root …          # 얼마나 했는지 (분할별 포함)
minedocscan review export-answers answers.json --split test --site … --work-root …
minedocscan eval --answers answers.json --target raw --only-listed --split test --work-root …   # 기계 값을 검수값과 비교
minedocscan review export-crops ~/crops --split train --kind handwritten_number --include-illegible --site … --archive-root … --work-root …
                                            # 학습용 크롭 (저장소 밖). illegible 은 숫자 인식기의 "거절"로 학습한다

# 숫자 인식기 (docs/DATA.md "숫자 인식기") — 학습만 torch, 추론은 OpenCV
minedocscan recognizer train --crops ~/crops --name digits-v1      # → <site>/models/digits-v1/ (test 줄이 있으면 거절)
minedocscan recognizer eval  --crops ~/crops --model digits-v1 --split val --errors   # 검증 날짜에서, 틀린 칸 모아 보기는 WORK_ROOT
minedocscan recognizer list                                         # 사이트 팩의 모델과 카드 요약
# 설정: [recognize.by_kind] handwritten_number = "digits"   [recognize.digits] model = "digits-v1"   → minedocscan info 로 확인
minedocscan synth out/low --low-cells       # 낮은 칸·거친 숫자·X 표의 합성 양식 (숫자 인식기 시험용)

# 표 밖 필드(쪽 메타: 차량번호·작성자·월·일) — docs/DATA.md "쪽 메타", ADR 0013·0014
minedocscan review export-crops ~/meta --meta --split train --site … --archive-root … --work-root …   # 사람·파일명 값이 있는 필드만
minedocscan recognizer train --crops ~/meta --meta-key vehicle_no --name veh-v1 --cv 5    # 숫자: 읽고 닫힌 목록에서 고른다
minedocscan recognizer train --crops ~/meta --meta-key operator   --name op-v1  --cv 5    # 이름: 닫힌 집합 분류기
minedocscan recognizer eval  --crops ~/meta --model op-v1 --split val --errors            # --cv 모델은 묶음 교차 읽기(cv-reads.jsonl)로
# 설정: [recognize.meta] vehicle_no = "veh-v1"  operator = "op-v1"   → minedocscan info
minedocscan eval --meta --split test        # 키마다 정확도·자동 적재 오류율·배차가 바뀐 쪽·자리
minedocscan pages --meta-mismatch [--meta-key date.day]   # 기계 값이 사람·파일명 값과 다른 쪽 (값은 찍지 않는다)
minedocscan review serve … --queue page-fields --audit 100 --reviewer jp   # 표본 감사 (기계 값 없이)
minedocscan review serve … --queue meta-check --reviewer jp               # 기계 값 ≠ 라벨·검수
minedocscan synth out/meta --meta-fields [--mix-pages]   # 사람마다 다른 획의 메타 필드 합성 (새 차·새 사람·바꿔 탄 날)

# ✓ 판정의 정답
minedocscan review serve … --queue checks --n 300 --reviewer jp   # 1 유 / 2 무 / Enter 표시 없음 / ? 모름 → 두 칸의 검수 두 건
minedocscan eval --checks [--split test]    # 기계의 답 × 정답 표, 정확도(구간), 판정 불가, column_unused

# 장비 가동 일보 (tasks/0005, ADR 0015·0016) — 값의 형식, 표의 역할, 계기 검산
minedocscan template check   <site>/templates/<양식>          # 오류를 전부 목록으로 (역할에 필요한 칸, 형식과 종류, 겹치는 칸, 쪽 밖 …)
minedocscan template preview <site>/templates/<양식> [--scan F --page N]   # 칸·필드·형식·역할을 그린 PNG → WORK_ROOT/template-preview
minedocscan review serve … --queue readings --reviewer jp      # 가동 시간 칸: 쪽마다 계기(시작·종료·총) + 근무 시각을 한 번에
                                            # (기계 값·앞날 값 없이, --audit N). 계기의 시작·종료 칸에 08.00 을 넣으면 시각인지 묻는다
                                            # (usage-check·pending 에서도)
minedocscan review serve … --queue usage-check --reviewer jp   # 계기가 이어지지 않는 곳: 두 칸을 같이 — 고치면 빠지고, 고치지 않으면 확인
minedocscan synth out/usage --usage-logs [--usage-only]       # 가동 일보 두 종 (하루 두 장, 빠진 날, 시각, 빈 계기, 대응표에 없는 이름 …)

# 인쇄 층·같은 날 섞여 쓰이는 판·표 더하기 (tasks/0006, ADR 0017·0018) — 가동 일보 템플릿을 만드는 순서는 docs/SITE_PACK.md
minedocscan template print-layer <site>/templates/<양식> [--percentile 50]   # 분류된 쪽들에서 인쇄 층 → <양식>/print.png + 요약
                                            # (분류 전용 쪽도 직접 정합, 3장 미만 거절, 5장 미만 경고). print_image: print.png 는 사람이 적는다.
                                            # 판이 섞였을 수 있는 양식의 첫 층은 --percentile 50 — 괘선을 잡는 데(add-region)만 쓴다.
                                            # 값 유무에 쓰는 층(print_image 로 run)은 판을 나눈 뒤 판마다 75 로 다시 (75 미만이면 요약이 경고)
minedocscan template preview <site>/templates/<양식> --print            # 인쇄 층 위에 칸 — 값 자리가 인쇄·잔상에 덮이지 않았나
minedocscan template add-region <site>/templates/<양식> --roi x0,y0,x1,y1 --name meter --role meter   # 표 하나의 뼈대 (인쇄 층이 있으면 거기서 괘선)
minedocscan template variant <site>/templates/<양식> --scan F --page N --name <양식>_b   # 다른 인쇄 판: 표마다 괘선만 다시 잡는다
                                            # 두 판에 같은 family 와 concurrent: true (명령이 안내한다, 기존 판은 고치지 않는다)
minedocscan pages --variants                # 두 판의 괘선 오차 차이가 1 px 미만인 쪽 (가르기 어려웠던 쪽, 판마다의 오차)
minedocscan synth out/usage --usage-only --print-layers --usage-variants   # 인쇄 층(합성 쪽에서 추정)을 넣은 가동 일보 + 판 B
                                            # print-layer·add-region·variant·preview 는 저장소 안(out/)을 거절한다 — 해 보려면 저장소 밖 경로로

# 접수 (tasks/0007, ADR 0019·0020) — 스캐너 폴더에서 업무 테이블까지
# 설정: [paths] inbox = "…/스캐너 저장 폴더" (또는 MINEDOCSCAN_INBOX). archive_root 에는 intake/ 아래에만 쓴다
minedocscan serve --reviewer jp             # 접수 폴더 감시 + 운영 화면 (127.0.0.1:8765): 홈(할 일·최근 문서), 문서 화면(날짜·버리기·다시 스캔)
minedocscan watch [--once] [--settle-seconds 0 --give-up-seconds 0]   # 화면 없이 감시만. --once 는 한 바퀴(접수 + 대기 중인 문서 처리)
minedocscan doc list [--status needs_date]  # 문서: 받은 시각, 쪽 수, 날짜, 상태, 다시 처리 대기
minedocscan doc date <문서 ID | 쪽 ID> 2030-01-09 --reviewer jp   # 결정을 남긴다 (reviews/decisions.jsonl) — 처리는 watch·serve 가
minedocscan doc discard|restore <문서 ID | 쪽 ID> --reviewer jp ;  minedocscan doc keep <쪽 ID> --reviewer jp   # keep = 다시 스캔이 아니다
minedocscan pages --rotated | --status blank | --status duplicate    # 돌아서 들어와 세운 쪽, 빈 쪽, 다시 스캔으로 붙잡힌 쪽(앞쪽과 유사도)
minedocscan template init <이미지> --name <이름> --rotate 90          # 돌아간 스캔으로 템플릿을 만들 때 기준 이미지를 세운다
minedocscan synth out/intake --intake       # 합성 접수 폴더 OUT/inbox + 견줄 묶음 OUT/baseline, 넣을 결정은 truth.json 의 intake.decisions
                                            # (--rotate-pages, --blank-backs, --rescans 는 따로도)

minedocscan run DB_scans --skip-existing    # 전체 묶음: 깨진 파일은 failed 로 격리, 한 것은 건너뜀 (템플릿·인식기를 바꾼 뒤엔 --fresh)
minedocscan report --by-month               # 양식 × 월 진단 (개정판의 흔적)
minedocscan pages --status unknown_form --thumbs   # 양식을 못 찾은 쪽 + 미리보기 (WORK_ROOT/thumbs)

# 실데이터 (저장소 밖 — docs/DATA.md)
export MINEDOCSCAN_SITE=…/site-packs/<현장>  MINEDOCSCAN_ARCHIVE_ROOT=…/mine-docscan  MINEDOCSCAN_WORK_ROOT=…/work
minedocscan info            # 설정·사이트 팩 (양식마다 인쇄 층·동시 판, 대응표의 해시 — 이름 없이)
minedocscan regress         # 사이트 팩의 기준 수치와 비교 (pytest -m realdata 도 같은 검사)
```

## 여섯 가지 원칙

1. **양식은 데이터다.** 양식 하나 = 사이트 팩의 `template.yaml` + 기준 이미지. 새 양식 때문에 코드를 고치지 않는다.
   코드를 고치는 것은 새 *종류*의 업무 기록(핸들러)이 필요할 때뿐이다.
2. **인쇄된 값은 읽지 않는다.** 장비명·광종·편처럼 양식에 인쇄된 값은 템플릿에서 확정한다. OCR 대상은 손으로 쓴 것뿐이다.
3. **모든 값은 출처를 가진다.** `doc_field` 에 페이지·좌표·원문·최종값·신뢰도·후보를 남긴다. 업무 테이블의 값은
   `source_field_id` 로 그 자리까지 거슬러 올라갈 수 있어야 한다.
4. **교정은 선택이지 생성이 아니다.** 교정기는 후보 중에서 고른다. 등록번호·차량번호 같은 고유값은 교정하지 않고 마스터와 맞춘다.
5. **사람은 예외만 본다.** 확신이 없는 값은 `review_status = pending` 으로 보내고 나머지는 자동 적재한다. 검수 결과는 학습 데이터가 된다.
6. **인식기는 갈아 끼우는 부품이다.** 파이프라인은 `Recognizer` 인터페이스만 안다. 백엔드는 같은 평가셋의 수치로 비교해 바꾼다.

## 패키지 지도 (`src/minedocscan/`)

| 위치 | 하는 일 |
|---|---|
| `config.py` | 설정 (환경변수 > TOML > 기본값). 경로를 코드에 적지 않는다 |
| `imaging/io.py` | 이미지·PDF 읽기/쓰기. **한글 경로 때문에 `cv2.imread/imwrite` 를 직접 쓰지 않는다**. PyMuPDF 로 읽고 렌더링하는 곳은 전부 `PDF_LOCK` 안 (serve 의 두 스레드 — `load_pages` 는 쪽을 내주는 동안 놓는다) |
| `imaging/grid.py` | 표 괘선 검출 |
| `imaging/align.py` | ORB + RANSAC 으로 기준 이미지에 정합, 괘선 재검출 오차로 품질 판정. 펴기는 `warp_to_template` 하나 (인쇄 층의 다시 펴기도 같은 그림). `align_upright`: 호모그래피의 회전각이 90° 단위로 0 이 아니면 `np.rot90` 으로 세워 다시 정합(저장하는 호모그래피는 원래 쪽 → 템플릿) |
| `imaging/signature.py` | 다시 스캔한 쪽의 서명(ADR 0020): 정합 그림 → binarize → 지울 자리(인쇄 + 표 밖 필드, `Template.signature_mask`) 0 → 2×2 열기 → 16 px 칸의 잉크 수, 코사인 유사도, base64 글자열. DB 를 모른다 |
| `imaging/cells.py` | 셀 크롭과 잉크 비율. 인쇄 마스크를 받으면 role 표 칸의 잉크는 이진화한 뒤 인쇄를 지운 그림으로 잰다 (크롭은 늘 원래 그림) |
| `imaging/marks.py` | ✓ 판정 (나란한 두 칸 중 어디에 표시했나) |
| `imaging/blobs.py` | 괘선 제거 + RLSA 로 글씨 덩어리를 셀에 배정, 여러 칸에 걸친 메모 구분 (`print_mask` 를 받으면 표 영역의 이진 그림에서 인쇄를 지운다) |
| `imaging/printlayer.py` | 인쇄 층(ADR 0017): `estimate`(쪽마다 erode 3×3 → 화소마다 밝기의 백분위, 기본 75, 보간 없이 — `method="higher"`), `binary`(`grid.binarize`), `mask`(2 px 넓힘 — 값 유무에서 지우는 자리), `sha`(화소의 해시), `coverage`. DB 를 모른다 |
| `imaging/cropspec.py` | 인식기에 넘기는 크롭의 규격(`CropSpec`: 해상도 aligned/source·배율·여유)과 자르는 구현 하나 — 파이프라인·내보내기·검수 화면이 같이 쓴다 |
| `imaging/hires.py` | 원본 쪽 렌더링 (몇 장 캐시). 원본 해상도 크롭은 쪽의 호모그래피로 그 셀만 다시 정합 |
| `forms/template.py` | 템플릿 로더·검증 (`meta_key`, `format`, 표의 `role`, 나눔 선 `split_ys`/`split_xs`, `subtotal`, `family`/`valid_from`/`valid_to`, `concurrent`). `problems()` 는 오류 전부. 인쇄 층 `print_image`(템플릿 폴더 안의 파일 이름만, PNG, 기준 이미지와 같은 크기 — `print_problems`·`print_layer`·`print_mask`·`print_sha`). YAML 의 문법·날짜 오류도 `TemplateError` (`load_yaml`). 인쇄 층을 쓰는 칸은 `role_value_cell` 한 곳(meter·shifts·tally 표의 형식 있는 손글씨 칸), `uses_print_layer` |
| `forms/formats.py` | 값의 형식(ADR 0015): `integer`·`decimal`·`time`·`time_range`·`reading`. 정규화 한 곳 — 검수 저장·서버·정답 내보내기·평가·핸들러가 같이 쓴다. 화면이 받는 글자와 안내. `dotted_clock`(점으로 쓴 시각 — 화면이 묻는 조건, 정규식과 상한은 `dotted_clock_rule()` 로 서버가 화면에 보낸다 — 화면에 수가 없다) |
| `forms/equipment.py` | 장비 마스터(점검표 템플릿의 장비 행), `equipment_id`, 장비명 메타 키 `equipment` |
| `forms/sitepack.py` | 사이트 팩 (템플릿·현장 옵션·페이지 라벨·평가셋 소금값), `templates_for(date)`, 장비명 대응표 `[equipment.aliases]`(마스터에 없는 키면 오류), `known_values(key)`(후보 목록), 대응표의 해시 `equipment_aliases_sha`. 동시 판(ADR 0018): 계열의 겹침은 모두 `concurrent` 일 때만, 계열에 동시 판 하나뿐이면 오류, 판끼리 기하 밖의 전부가 같아야 한다(`variant_key_diff`), 같은 `name` 둘이면 오류, `concurrent_groups(date)`, `answer_key`(정답은 계열로), `variant_families()` |
| `forms/classify.py` | 페이지가 어느 양식인지 (그날 유효한 판만 후보). 그날의 동시 판 묶음(`groups`)은 한 후보 — 점수는 최댓값, 1위/2위 여유는 계열 사이, `ClassResult.group` |
| `recognize/` | 인식 백엔드 인터페이스와 등록소 (`null`, `oracle`, `digits`), 칸 종류별 백엔드(`ByKindRecognizer`, `[recognize.by_kind]`) |
| `pagemeta.py` | 쪽 메타(`doc_page_meta`): 키마다 최종 값과 출처(검수 > 결정 > 라벨 > 파일명 > 기계 값), 기계 값의 대조, 날짜의 월·일 대조. 날짜의 순서 한 곳 — `page_date`(쪽의 결정 > 문서의 결정 > 쪽 라벨 > 문서 라벨 > 파일명)·`document_date`(ISO 라벨만), 사람의 출처 목록 `HUMAN_SOURCES` |
| `recognize/digits/` | 숫자 인식기: `model.py`(전처리·CTC 빔 탐색·ONNX 를 cv2.dnn 으로, torch 없음), `backend.py`(카드의 규격·온도·기준), `data.py`(크롭 폴더, test 거절, 검증 날짜), `calib.py`(온도·임계값표·윌슨 구간), `train.py`(학습 — torch 는 여기서만), `evaluate.py`(크롭 단위 평가) |
| `recognize/meta/` | 메타 필드 모델 (ADR 0013): `model.py`(카드·`classes.json`·후보 목록, `[recognize.meta]` → `build_meta_readers`), `choose.py`(CTC 우도로 닫힌 목록에서 고르기, 목록에 없는 값), `calib.py`(온도·기준·묶음 교차 읽기 `cv-reads.jsonl`), `train.py`(`--cv K`, 숫자 모델), `evaluate.py` |
| `recognize/choice/` | 이름 필드의 닫힌 집합 분류기 (종류 0 = "그 밖"): `model.py`(OpenCV 추론), `train.py`(torch) |
| `correct/` | 교정 백엔드 인터페이스와 등록소 (`none`) |
| `handlers/` | 양식의 의미: 셀 → `doc_field` → 업무 테이블 (`generic`, `inspection`, `haul`, `usage`). 숫자 칸의 자동 적재 표는 `base.number_status` (ADR 0012), 정수 칸의 행은 `base.number_row`(운반·작업량), 읽지 않는 형식은 `base.unread_row` |
| `handlers/usage.py` | 장비 가동 일보(ADR 0016): 표의 역할 `meter`·`shifts`·`tally`·`activities` → `eq_usage_daily`(쪽 하나에 한 행, 가동 시간과 근거) + `prod_tally`. 업무 행은 `usage_rows` 하나(load·on_review). 값 유무는 `presence`(덩어리 배정 또는 잉크 비율, 인쇄 마스크 — 핸들러와 시험이 같은 함수) |
| `validate/crosscheck.py` | 양식 간 교차검증, 그날의 실제 배차 관측 (날짜 지정 재계산 가능) |
| `validate/usage.py` | 가동 일보의 검산 → `xcheck_usage`: 쪽 안(총 = 종료 − 시작, 소계 = 합), 계기의 연속성(같은 장비의 어제 종료 = 오늘 시작). 장비 단위 재계산 |
| `review/` | 검수: `store.py`(추가 전용 `reviews.jsonl` ↔ `doc_review`, `save()`), `queue.py`(대기열 8종: `haul-numbers`·`mismatch`·`pending`·`page-fields`(`--audit`)·`meta-check`·`checks`·`readings`(계기 + 근무 시각 칸, `--audit`)·`usage-check`, 계기 시작·종료 칸의 `ask_dotted`), `checks.py`(✓ 행의 답 ↔ 두 칸의 판정), `crops.py`(원본/정합, 두 칸 띠), `export.py`(크롭 내보내기, `--meta`), `server.py` + `static/index.html`(표준 라이브러리, 127.0.0.1), `ops.py` + `static/home.html`(운영 화면 — 홈·문서 화면·결정, `serve` 에서만: 남은 수는 DB 가 바뀔 때만 다시 센다, 표본 대기열은 만들지 않는다, `/page.png` 는 원본에서 세워서) |
| `store/` | `schema.sql`, `upsert()`(`insert_only` — `received_at`), 스키마 버전, WAL·`write_txn`(`BEGIN IMMEDIATE`), 쪽을 가리키는 테이블 `PAGE_TABLES`(지우는 순서). `order.py`: 문서·쪽의 순서(접수한 문서는 뒤, 보관 경로의 성분 — 파이썬에서 견준다), `document_id`(해시) |
| `intake/` | 접수(ADR 0019): `inbox.py`(다 쓰인 파일만 — 수정 시각 + 열린다, 보관 폴더 `intake/<해-달>/<받은 시각>-<문서 ID>/` 로 복사·확인·등록·커밋 뒤에 치운다, `_already`·`_failed`, 시계 주입), `worker.py`(한 바퀴 = 접수 + 대기 문서 처리, `run_forever`, 작업 상태, 요약은 수와 문서 ID 만), `decisions.py`(추가 전용 `decisions.jsonl` ↔ `doc_decision`, 저장은 전부 검사한 뒤, `dry_run`), `dates.py`(사람이 넣는 날짜) |
| `pipeline/runner.py` | 단계 순서와 상태 기록만 안다. 등록과 처리(`process_document` — 지우고 다시 만든다, 쪽마다 커밋, 요청 번호 `work_requested > work_done`, `needs_date`·`discarded`), 대기 문서 처리(`process_pending`, 문서의 순서대로), 빈 쪽(`blank_max_ink`), 다시 스캔(`dup_min_sim` — 같은 날·계열의 앞 순서 적재된 쪽, 뒤 문서에 다시 요청). 오류 격리(`failed`/`error`), `--skip-existing`. 동시 판 묶음이면 판마다 정합해 괘선 오차로 고른다(`choose_variant` — 0.5 px 안이면 인라이어, 그다음 이름; `doc_page.variant_errs`). 인쇄 층을 쓰는 양식(`uses_print_layer`)이면 마스크를 칸·핸들러에 넘긴다(`doc_page.print_sha`) |
| `evaluate/` | CER·필드 정확도·자동 적재율·자동 적재 오류율(`status_raw`), 값 유무 정밀도·재현율, 날짜 분할(`split.py`), 비율의 구간(`stats.py`), 쪽 메타(`meta.py`), ✓ 판정(`checks.py`), 실데이터 회귀(검수 없이, 기준에 없던 묶음은 따로 알림) |
| `pipeline/lock.py` | 파이프라인은 한 번에 하나 (DB 옆 `pipeline.lock` 에 배타 트랜잭션 — 죽은 프로세스의 잠금이 남지 않는다). 명령이 잡는다 |
| `report.py` | DB 현황 요약 (회귀 테스트가 비교하는 수치), `by_month`, `list_pages`. 인쇄 층으로 잰 쪽(`print_layer`)과 계열별 판(`variants` — `variant_summary`)은 그런 쪽이 있을 때만, 점으로 쓴 시각일 수 있는 쪽(`usage_dotted_suspect`)은 가동 기록이 있으면(0 이어도) 키가 생긴다. 낡은 장비 ID(`stale_equipment_ids` — 리포트 밖, 회귀가 비교하지 않는다, 가동 기록·작업량 행이 있을 때만) |
| `tools/synth.py` | 합성 양식·스캔·정답 생성기 (같은 seed 면 바이트까지 같다, 행렬 개정판 선택, `low_cells` 낮은 칸 양식, `usage_logs` 가동 일보, `print_layers` 합성 쪽에서 추정한 인쇄 층, `usage_variants` 판 B, `rotate_pages`·`blank_backs`·`rescans`·`intake` — 따로 쓰는 난수, 기존 선택의 바이트는 그대로) |
| `tools/synth_usage.py` | 합성 가동 일보 두 종: 계기(소수·시각·빈 칸), 하루 두 장, 빠진 날, 잘못 적은 시작, 총·소계 어긋남, 대응표에 없는 이름, 작업량 표 위의 메모. 근무 시각 칸의 인쇄된 "~"(선으로 그린다). 판 B(`build_usage_log("b")` — 작업 표·계기 표만 10 px 아래, 줄 간격 ×1.01). 난수는 따로 |
| `tools/tpltools.py` | `template preview`(칸·필드·형식·역할을 그린 PNG, `--print` 는 인쇄 층 위에, 저장소 밖에만), `template check`(오류를 전부 — 인쇄 층의 크기, 인쇄에 절반 넘게 덮인 표 칸; 쓰이지 않는 `print_image` 는 참고 줄) |
| `tools/printlayer.py` | `template print-layer`: 그 양식으로 분류된 쪽(loaded·classified_only)을 날짜별로 고르게 최대 40장 — 정합 그림, 없으면 호모그래피로 다시 펴고, 분류 전용 쪽은 직접 정합(인라이어만). 3장 미만 거절·5장 미만 경고·백분위 75 미만이면 괘선을 잡는 데만 쓰라고 경고. DB 는 읽기 전용, `print.png` 만 쓴다. 요약은 수와 칸 이름만 |
| `tools/variant.py` | `template variant`: 표 영역(+60 px) 밖의 특징점만으로 편 그림이 새 기준 이미지(`header_homography`), 표마다 기존 괘선을 ±min(40 px, 이웃 표까지의 절반 — 붙은 표가 같이 쓰는 경계선(3 px 안)은 빼고 잰다) 안에서 다시 잡아 짝짓는다. 열·행·필드는 그대로, 기존 판은 고치지 않는다 |
| `tools/synth_meta.py` | 메타 필드 합성: 사람마다 다른 획(기울기·굵기·크기·간격)의 네 자리 차량번호·이름·월·일, 크롭 폴더 (메타 모델의 학습·시험용) |
| `tools/synth_cells.py` | 어려운 합성 숫자 칸: 값·X 표·덧칠·메모·이웃 칸 글씨를 크롭 규격대로 (숫자 인식기의 학습·시험용) |
| `tools/handfont.py` | 합성 손글씨의 획 정의 (숫자 꼴 몇 가지, 소수점·콜론·물결표·붙임표, 메모용 이어 쓴 글자). OpenCV 내장 글꼴을 쓰지 않는다 |
| `tools/thumbs.py` | 쪽 미리보기 (1/4, WORK_ROOT/thumbs — 방향을 알면 세워서, 파일 이름에 문서 ID) |
| `tools/mktemplate.py` | 새 양식의 템플릿 뼈대(`template init`)와 표 더하기(`template add-region` → `add_region`: 인쇄 층이 있으면 그것에서 괘선, `regions` 블록 끝에 글자로 끼워 넣고 다시 읽어 확인, 아니면 되돌린다). 둘이 같은 뼈대 `region_skeleton`(`--role` 의 자리표시)을 쓴다 |
| `cli.py` | `minedocscan` 명령 |

## 작업 규칙

**변경은 수치로 판단한다.** 인식·정합·판정 규칙을 고쳤으면
1. `pytest` — 합성 데이터에서 정답과 정확히 같아야 한다.
2. 실데이터가 있는 환경이면 `minedocscan regress` — 기준과 달라진 수치를 확인하고, 좋아진 것이면
   `--update` 로 기준을 갱신하고 무엇이 왜 바뀌었는지 커밋 메시지에 적는다. 수치가 달라졌는데 설명이 없으면 안 된다.
3. 인식 백엔드를 붙이기 전에 `oracle` 백엔드로 CER 0 을 확인한다. 0 이 아니면 인식기가 아니라 파이프라인의 버그다.

**개인정보를 커밋하지 않는다.** 현장 문서에는 작업자 이름, 서명, 차량번호가 있다.
- 스캔 원본, 기준 이미지, 템플릿 YAML(머리글에 이름·차량번호가 들어 있다), 페이지 라벨, 정답 CSV·엑셀, 검수 기록(`reviews.jsonl`)은 전부 저장소 밖(사이트 팩·아카이브)에 둔다.
- 검수 화면의 갈무리(실제 값이 보인다)를 문서·PR·이슈에 붙이지 않는다. 서버 로그에 입력값을 찍지 않는다.
- 테스트에 필요한 이미지는 `tools/synth.py` 로 만든다. 실제 문서를 `tests/fixtures/` 에 넣지 않는다.
- 학습한 모델(`<site>/models/` — 메타 필드 모델의 `classes.json` 은 이름·차량번호 목록이다), 내보낸 크롭, 틀린 칸 모아 보기는 현장 글씨다 — 저장소 밖에.
  예외는 합성 데이터만으로 만든 시험용 모델 셋: `tests/fixtures/digits-fixture`, `meta-digits`, `meta-operator` (다시 만드는 명령은 `tests/fixtures/README.md`).
- 인쇄 층(`print.png`)과 판의 기준 이미지(`template variant` 가 쓰는 `reference.png`)도 현장 데이터다 — 저장소 밖에. 늘 같은 자리에 같은 글씨로
  쓰는 칸(작성자 이름, 늘 "ok" 인 점검란, 서명)은 쪽이 많아도 인쇄 층에 손글씨의 잔상이 남는다. `template print-layer`·`variant`·`add-region`·
  `preview` 는 저장소 안을 거절한다. 예외는 합성 양식으로 시험 중에 만든 것뿐이다 (저장소에 넣지 않는다).
- 카드·로그·오류 메시지·리포트에 이름·차량번호를 찍지 않는다 — 종류의 수와 분포만. 값은 검수 화면(127.0.0.1)에서만 본다.
- 문서·코드·커밋 메시지·이슈에 실제 이름이나 차량번호를 예시로 쓰지 않는다. 합성 데이터의 값(`T01`, `V-101`, `ALPHA`)을 쓴다.
- API 키·비밀값은 환경변수로만 받는다. `.env`, `minedocscan.toml` 은 커밋되지 않는다.

**코드 규약**
- Python 3.11+, `ruff` (줄 길이 110). 주석·독스트링·문서는 한국어, 식별자는 영어.
- 좌표는 전부 템플릿 좌표계(기준 이미지 픽셀, 200 dpi). 페이지 좌표를 따로 들고 다니지 않는다.
- DB 쓰기는 `store.db.upsert()` 만 쓴다. 같은 문서를 다시 돌려도 행이 늘지 않아야 한다(멱등).
- 스키마는 SQLite 와 PostgreSQL 에서 같이 도는 문법만 쓴다. 날짜는 ISO 문자열, 불리언은 0/1.
  컬럼이 바뀌면 `store/db.py` 의 `SCHEMA_VERSION` 을 올린다 (마이그레이션 없음, 예전 DB 는 `--fresh`).
- 기계 값(`value_raw`, `has_value_raw`, `trips_raw`, `confidence`, `backend`)은 검수가 건드리지 않는다. 업무 테이블은 검수를 적용한 최종 필드 행에서 만든다 (ADR 0008).
- 검수 기록의 원본은 사이트 팩의 `reviews/reviews.jsonl` 이다. 지우거나 덮어쓰지 않는다. 테스트에서는 `reviews` 경로를 `tmp_path` 로 돌린다.
- 결정 기록(`reviews/decisions.jsonl` — 날짜·버리기·되살리기·keep)도 추가 전용 원본이고 `doc_decision` 은 사본이다. 결정은 업무 테이블을 직접
  고치지 않는다 — 그 문서의 요청 번호를 올리고, 처리가 지우고 다시 만든다 (적용하는 곳은 하나). `regress` 는 결정을 읽고 검수는 읽지 않는다.
- **원본을 지우지 않는다.** 접수 폴더에서 치우는 것은 보관 폴더의 사본을 해시로 확인하고 등록·커밋한 뒤에만. `archive_root` 에는 `intake/` 아래에만 쓴다.
- 쓰는 트랜잭션은 `store.db.write_txn`(`BEGIN IMMEDIATE`)으로 시작하고 읽는 것부터 그 안에서 한다. 처리는 쪽마다 커밋한다.
- **순서에 기대지 않는다.** 행이 들어간 순서나 SQL 의 `ORDER BY` 글자 순서 대신 `store/order.py` 의 문서·쪽 순서로 정렬한다. 불변식: 문서를 몇 번을
  어떤 순서로 다시 처리하든 지금의 DB = 같은 파일·검수·결정으로 처음부터 만든 DB. 쪽을 가리키는 업무 테이블을 더하면 `PAGE_TABLES` 에도.
- `watch`·`serve` 의 로그·요약과 새 오류 메시지에는 수와 문서 ID 만 — 파일명·날짜·메모를 찍지 않는다. 시험은 시계를 주입하고 잠들지 않는다.
- 판정 규칙의 숫자(임계값)는 근거를 주석으로 남긴다. 실데이터에서 어떤 경우 때문에 그 값이 되었는지.
- 현장에 관한 것(장비 구분, 파일명 규칙, 제외할 광종)을 코드에 적지 않는다. 사이트 팩의 `site.toml` 로 보낸다.

## 확장하는 법 (요약)

- **새 양식** (같은 종류의 기록): `minedocscan template init <이미지> --name <이름> --roi …` → `template.yaml` 의 열·행을 채운다 →
  `template check`·`template preview` 로 확인. 코드 변경 없음. `docs/SITE_PACK.md`.
  가동 일보는 표마다 `role`(meter·shifts·tally·activities), 칸마다 `format`, 장비명 대응표 `[equipment.aliases]`.
  만드는 순서: 분류 전용으로 돌린다 → `template print-layer` → `print_image` → `template add-region` → 열·행·필드 → `check`·`preview --print`
  → 같은 날 섞여 쓰이는 판이 있으면 `template variant` (두 판에 같은 `family` + `concurrent: true`). `check` 가 통과할 때까지 그 사이트 팩으로 `run` 하지 않는다.
- **새 종류의 업무 기록**: `handlers/<이름>.py` 에 `FormHandler` 를 상속해 `load()` 를 쓰고 `handlers/__init__.py` 의 `REGISTRY` 에 등록,
  `store/schema.sql` 에 테이블과 `store/db.py` 의 `PRIMARY_KEYS`(쪽을 가리키면 `PAGE_TABLES` 도) 를 추가, `tools/synth.py` 에 그 양식의 합성판과 테스트를 추가.
- **새 인식 백엔드**: `recognize/<이름>.py` 에 `recognize(crops, contexts) -> list[Recognition]` 을 구현하고 `register()`.
  원하는 크롭은 `crop_spec`/`crop_spec_for(kind)` 로 선언한다 (ADR 0011). 답의 종류(값·빈 칸·거절)를 말하면 `Recognition.answer` 에.
  무거운 의존성(torch 등)은 그 모듈 안에서만 import 하고 `pyproject.toml` 의 선택 의존성으로 넣는다. 추론은 현장 PC 에 새 의존성 없이 (ONNX + OpenCV).
- **새 교정 백엔드**: `correct/` 에 같은 방식으로. 후보를 내고 고르는 구조를 지킨다.

## 실데이터에서 배운 것 (다시 틀리지 않기 위해)

- ✓ 는 왼쪽 칸에서 시작해 경계선을 넘어 오른쪽 칸까지 그려진다. 잉크량 비교나 꼭짓점 위치로는 틀린다 → `imaging/marks.py` 의 규칙.
- 점검을 하지 않는 날(예: 토요일)은 체크 열 전체가 빈다. 행마다 "판정 불가"로 두지 말고 페이지 단위로 `column_unused`.
- 가로 양식은 한 페이지에 표가 여러 개라 페이지 전체 괘선 검출이 짧은 괘선을 놓친다 → 표(region)별 ROI 검출.
- 정합 품질은 표별 괘선 오차의 **중앙값**으로 본다. 스캔 가장자리에서 잘린 괘선 하나가 평균을 망친다.
- 표 위에 여러 칸에 걸쳐 쓴 메모는 셀 값이 아니다 → `imaging/blobs.py` 가 덩어리 폭으로 구분한다.
- 행렬 양식에 인쇄된 운전자·차량번호는 양식을 만든 당시 상태로 굳어 있다. 사람도 차도 바뀐다.
  두 양식을 잇는 키는 행렬의 **열 자리(slot)** 이고, 그날의 실제 배차는 `eq_assignment_obs` 에 관측값으로 남긴다.
- 하루에 행렬 양식이 여러 장일 수 있다(상차 장비마다 한 장). 그날의 모든 장을 합쳐서 비교한다.
- 양식의 여백 행에 손으로 장비를 추가해 적는 날이 있다. `doc_field` 에는 남지만 업무 테이블로 가는 규칙은 아직 없다.
- PDF 는 200 dpi 회색조로 직접 렌더링한다. 렌더링 경로를 바꾸면 체크 판정 몇 개가 뒤집힌다 — 회귀 기준을 다시 잡아야 한다.
- OpenCV 5.0 은 내장 글꼴(`putText`)의 모양을 바꿨다. 그 글꼴로 그린 합성 숫자로 학습한 시험용 모델이 4.x 에서 시험 4개를 떨어뜨렸다 (추론은 판마다 같았다).
  학습·시험에 쓰는 합성 글씨는 자체 획(`tools/handfont.py`)으로 그리고, CI 는 하한 판(OpenCV 4.9, numpy 1.26)에서도 돈다.
- 자동 적재 기준은 검증 칸이 적으면 아무 말도 하지 못한다 (13칸·오류 0 → 상한 23 %). 자동 적재된 검증 칸이 100개 미만이면 기준을 정하지 않고, 기준 옆엔 늘 상한 (ADR 0012).
- 스캔 원본은 300 dpi 다. 정합과 판정은 200 dpi 로 충분하지만, 인식기에 넘기는 크롭은 원본 해상도가 나을 수 있다 (실데이터로는 아직 비교하지 않았다 — 두 규격으로 따로 학습해 검증 날짜에서 본다).
- 운반 숫자 칸은 낮고 넓다 (괘선 사이 높이 28–30 px, 폭 약 100 px). **글씨가 칸보다 커서** 위아래 괘선을 넘는다 — 칸 그대로 자르면 숫자가 잘린다.
  크롭에 여유(행 높이의 절반)를 둔다.
- 그래서 **이웃 칸의 글씨가 이 칸으로 넘어온다.** 잉크로 "값 있음"이라 판정된 칸의 약 5분의 1은 이 칸의 숫자가 아니었다 (X 표, 덧칠, 윗칸 숫자의 꼬리, 메모).
  숫자 인식기는 "무슨 숫자인가"와 함께 "이 칸에 숫자가 있기는 한가"를 답한다 — 빈 칸과 거절 (ADR 0012).
- 값은 1–16 근처이고 두 자리가 드물지 않다. 흘려 쓰고, 연하고, 쓰는 사람이 여럿이다 — 값 전체를 분류 항목으로 두지 않고 숫자열(CTC)로 읽는다.
- 언어모델에 문장을 다시 쓰게 하면 긴 셀에서 항목 순서가 바뀌고 수량이 달라진다 (선행 연구의 비교표). 교정은 후보 선택 + 숫자 불변 검사.
- 합성 PDF 도 저장할 때 새 /ID 가 들어가면 같은 seed 인데 문서 해시가 달라져 테스트가 운에 따라 실패한다 → `no_new_id`.
- 회귀 검사는 검수 파일을 읽지 않는다. 검수가 쌓이면 코드 변경 없이도 `pending`·`with_trips` 가 달라진다.
- 한 묶음 안에서 양식이 개정되면 모양으로는 못 가린다(분류 여유 ≈ 1). 날짜로 가린다 (ADR 0010).
- 주간만 검수하고 야간은 아직인 칸은 합을 모르는 것으로 둔다. 아니면 일부 검수 중에 가짜 불일치가 생긴다.
- 일보의 **차량번호는 전부 네 자리 숫자**(11종)이고 "차량번호:" 뒤에 크게, 띄엄띄엄 쓴다. 여러 번호가 앞 두 자리가 같다.
  한 차의 번호는 거의 늘 같은 사람이 쓴다 — 분류기로 풀면 숫자가 아니라 글씨체를 외워, 다른 차를 탄 날에 평소의 차로 읽는다. 숫자를 읽고 목록에서 고른다 (ADR 0013).
- **작성자는 10명**이고 날마다 같은 사람이 자기 이름을 같은 글씨로 쓴다 — 이름은 글자보다 모양으로 고른다 (닫힌 집합 분류기).
- 날짜 줄은 "20__년 __월 __일 __요일" 이 인쇄되어 있고 월·일·요일만 손으로 쓴다. 연도는 인쇄된 값이고 묵은 양식에서는 틀려 있다(위에 덧쓴다).
  날짜는 파일명·라벨이 정하고, 손으로 쓴 월·일은 대조에만 쓴다.
- 메타 필드는 쪽마다 한 칸이라 정답이 적다 (20일치 ≈ 200쪽). 검증 날짜 20 % 로는 기준이 안 나온다 → 날짜 묶음 교차(`--cv 5`), 자동 적재된 쪽은 표본 감사 (ADR 0014).
- 괘선 제거가 세로획 하나짜리 "1" 을 거의 다 지운다 (잉크 비율이 0 에 가깝다). 모델이 있는 메타 필드는 잉크가 전혀 없을 때만 건너뛴다.
- `cv2.HOGDescriptor` 는 OpenCV 5.0 에 없다. OpenCV 의 부가 기능에 기대지 않는다.
- **가동 일보** 셋(중기운행일보·점보 작업일보·로우더 작업일보)은 장비 한 대의 하루다. 계기 칸에 가동 시간계의 값을 소수 한 자리로 적고(1234.5),
  "총"은 거의 비어 있다. 같은 장비의 어제 종료 = 오늘 시작이다 (네 대에서 이어졌다). 계기가 없는 장비는 같은 칸에 **시각**을 적는다 — 점으로 쓰기도 한다(08.00).
  1234.5 와 08.00 은 모양으로 갈리지 않는다 → 사람이 콜론으로 가른다 (ADR 0015). 34쪽 중 계기 12, 시각 3, 나머지 빈 칸.
- 작업 표는 자유롭게 쓴다 (작업내용에 시간대, 운행시간 칸에 이름, 〃 표시). 칸의 뜻대로 나눠 읽을 수 없다 → 글씨 있는 줄의 수만 업무 테이블에.
- 로우더 작업일보의 작업량 칸에는 "하단: _ 대 / 저광장: _ 대" 가 **인쇄되어** 있다 — 인쇄된 줄마다 행을 나누되, 그 선은 괘선이 아니다(`split_ys`).
  괘선 목록(`ys`)에 넣으면 정합이 없는 괘선을 찾다가 실패한다. 칸 안의 인쇄된 글자는 늘 잉크로 보인다.
  **양옆에 인쇄가 있는 칸("하단: _ 대")에서는 인쇄를 따로 떼어 `printed` 칸으로 두는 것으로 숫자를 잡지 못한다** — 쓴 숫자가 양옆의 인쇄와,
  인쇄는 이웃 칸의 인쇄와 이어져 덩어리 배정이 줄 전체를 메모로 보고 칸을 다 비웠다 (숫자를 쓴 칸 48개 중 0개; 인쇄를 나눔 선으로 떼어도
  한 자리 45/48, 두 자리 0/48). 작업량 칸은 덩어리 배정 **또는** 잉크 비율 중 하나라도 "있음"이면 인식기·검수로 (빈 칸 자동 적재 없음).
  인쇄 층을 켜면 두 판정을 인쇄를 지운 그림으로 잰다 — 아래.
- 계기 값은 길다(1234.5). 이웃한 칸의 값끼리 칸 폭의 절반보다 가까우면 덩어리 배정(RLSA)이 한 덩어리로 묶고, 폭이 1.6칸을 넘으면 메모로 판정해
  **두 칸 다 빈 칸**이 된다 → 가동 일보의 표 칸(읽지 않는 칸, 작업량 정수 칸)은 잉크 비율로도 보고, 둘 중 하나라도 "있음"이면 인식기·검수 대기
  (빈 칸 자동 적재로 값이 사라지지 않게). 운반(`haul`) 칸은 덩어리 배정만 그대로.
- 괘선 지우기는 세로획뿐인 글씨("1", "drill" 같은 낱말)를 거의 다 지운다 — 잉크 비율로 보는 작업 표 칸은 빈 칸이 된다. 합성 작업 표는 그런 글씨를 쓰지 않는다.
- 합성 글씨의 폭을 문턱(0.2)으로 재면 OpenCV 판마다 안티에일리어싱이 달라 1 px 차이가 나고 뒤의 글자가 다 밀린다 → 잉크 질량으로 잰다.
- **칸 안의 인쇄**(작업량의 "하단: _ 대", 근무 시각의 "~", 계기의 "시작:")는 잉크 판정이 손글씨와 가리지 못한다 — 인쇄가 있는 칸은 비어 있어도
  늘 검수 대기였다 (작업량의 인쇄 칸 48/48, 근무 시각의 빈 칸 9/9). 인쇄는 모든 쪽에서 같은 자리에 있고 손글씨는 쪽마다 다르다 →
  **인쇄 층**: 같은 양식의 정합 그림 여러 장에서 화소마다 밝기의 75 백분위, 보간 없이 (ADR 0017). 뺀 뒤에는 48/48 → 1/48, 9/9 → 0/9, 계기 칸의 빈 칸
  5/60 → 2/60 이고 값이 적힌 칸은 그대로 잡혔다. 백분위 50·75, 넓히는 폭 1–3 px 에서 같았다.
- 인쇄는 **이진화한 뒤 지운다.** 회색 그림에서 인쇄를 흰색으로 칠한 뒤 이진화하면 적응 이진화가 칠한 자리의 가장자리를 잉크로 잡는다
  (합성: 인쇄만 있는 작업량 칸 89칸 중 13칸이 여전히 "있음". 이진화한 뒤 지우면 넓히는 폭 0–3 px 모두 0칸).
- 인쇄 층은 **표 밖 필드와 크롭에 쓰지 않는다.** 날마다 같은 자리에 같은 글씨로 쓰는 칸(작성자, 늘 "ok" 인 점검란, 서명)은 쪽이 많아도 층에
  잔상으로 들어가 지워진다 (합성 점검란: 켜면 6쪽 중 6쪽이 빈 칸). 인쇄와 겹친 획도 지워진다 — 인식기와 사람은 원래 그림을 본다.
- **쪽이 적은 인쇄 층은 값을 지운다.** 같은 자리에 쓴 값의 잔상이 층에 남고 마스크가 그 자리를 지운다. 백분위를 보간하면 쪽이 적을 때
  잔상이 반쯤 남는다 (3장의 75 백분위 = 둘째·셋째 밝기의 중간): 합성에서 2장 층이 로우더의 두 자리 작업량 3칸, 3장 층이 4칸을 잃었고,
  `template add-region` 에서는 운행일보 4장 층의 잔상이 계기 시작 칸 안의 **덤 괘선**이 되었다. → 보간 없이(`method="higher"` — 3·4장이면
  가장 밝은 쪽) 잡으면 3–5장 0칸·덤 괘선 없음, 2장은 여전히 3칸이라 **3장 미만은 거절**한다 (5장 미만은 경고).
- 보간하지 않는 대가: 50 백분위도 한 판의 괘선을 그 판이 쪽의 **절반을 넘을 때만** 남긴다 — 두 판이 꼭 반씩이면(2+2, 5+5) 둘 다 빠진다.
  쪽 수를 홀수로(`--max-pages`) 하면 늘 한 판이 절반을 넘어 그 판의 괘선이 남는다.
- 인쇄 층은 정합의 기준 이미지로는 나쁘다 (인라이어 중앙값 1,315 → 777 — 여러 쪽을 겹친 그림이라 획이 무르다). 괘선을 잡는 데는 낫다 —
  채워진 스캔에서 잡으면 손글씨의 세로획이 괘선으로 섞여 나온다.
- **같은 날 섞여 쓰이는 인쇄 판**: 중기운행일보 30쪽 중 5쪽은 표가 8–12 px 아래, 줄 간격도 다른 묵은 판이다 — 날짜로 못 가리고, 모양으로도
  못 가린다 (1위/2위 비율 최소 1.01). 판 A 의 템플릿 하나로는 1쪽이 괘선 오차 5.5 px 로 **통과해 칸이 어긋난 채** 적재되었다. 괘선 오차로는
  갈린다 (A 25쪽, B 5쪽, 실패 0; B 의 쪽은 A 에서 5.5–10.5 px, B 에서 0.0–1.5 px) → 판마다 정합해 오차가 작은 판 (ADR 0018).
- 판이 섞인 양식의 인쇄 층을 75 백분위로 만들면 소수 판의 몫이 25 % 를 넘을 때 다수 판의 괘선이 층에서 빠진다 (판 A 의 가로 괘선이 남은 비율:
  A 의 몫 0.64 에서 0.55, 0.50 에서 0.44 — 50 백분위는 둘 다 1.00, 보간하던 때) → 판을 나누기 전의 첫 층은 `--percentile 50` 이고
  **괘선을 잡는 데(add-region)만** 쓴다. 값 유무에 쓰는 층(`print_image` 로 `run`)은 판을 나눈 뒤 판마다 75 로 다시 만든다 — 낮은 백분위의 층에는
  같은 자리에 쓴 값의 잔상이 더 남는다. `print-layer` 는 75 미만이면 요약에서 경고한다.
- 붙은 표(실제 운행일보의 작업 표와 계기 표)는 경계선 하나를 같이 쓴다. `template variant` 가 짝짓는 반경을 "이웃 표의 가장 가까운 괘선까지의
  절반"으로 재면 그 경계선 때문에 0.5 px 가 되어 괘선을 하나도 다시 잡지 못했다 → 이 표의 괘선과 3 px 안인 이웃 괘선은 빼고 잰다.
- 판의 템플릿을 쪽 전체의 호모그래피로 펴서 만들면 RANSAC 이 표 쪽으로 타협해 표를 기존 판 자리로 끌어오고 머리를 옮긴다 (다시 잡은 괘선이
  2–16 px 다르다) → 표 영역 밖의 특징점(머리·제목)만으로 편다.
- 로우더 작업일보는 계기가 비어 있고(6쪽 전부) 가동 시간이 근무 시각 칸에만 있다 → `readings` 에 근무 시각 칸.
- 계기 칸에 시각을 적은 3쪽(계기가 없는 장비 — 로우더 작업일보가 아니다) 중 2쪽이 점으로 썼다(08.00) — 적힌 대로 넣으면 계기 값 8.00 이
  되고 첫날에는 검산에도 걸리지 않는다 → 화면이 묻는다 (코드는 여전히 콜론만 시각으로 본다).
- OpenCV 내장 글꼴(Hershey)의 "~" 는 판마다 잉크가 크게 다르다 (5.0 에서 102 px, 4.9 에서 276 px). 합성 양식의 인쇄 표시 하나가 값 유무를
  정하면 글꼴이 아니라 선으로 그린다.
- **스캐너가 붙이는 이름은 날짜 규칙에 맞지 않는다.** 하루치(28쪽)를 그런 이름으로 넣으면 전부 `loaded`, 업무 행 1,773행이 `work_date` NULL,
  경고 없음이었다. 스캔한 날은 작업한 날이 아니다 → 날짜를 모르면 처리하지 않고 기다린다(`needs_date`), 사람이 종이의 날짜를 넣는다 (ADR 0019).
- **B5 가로 양식은 스캐너(급지 폭 216 mm)에 짧은 변부터 들어가 90° 돌아 있다.** 그대로도 전부 분류·`loaded` 지만 값 유무가 16–32칸(10,274칸 중)
  조용히 달라진다. 호모그래피에서 방향을 읽어(83쪽 × 네 방향 전부 맞았다) 정확히 세워 다시 정합하면 바로 선 쪽과 바이트까지 같다 (`align_upright`).
- **같은 종이를 다시 스캔하면 두 번 들어간다** (업무 행 두 배, 교차검증에 가짜 `missing_matrix` 200행). 손글씨 자리의 서명으로 같은 날 안에서는 갈린다
  (다른 종이 최대 0.66, 흔들어 다시 정합한 같은 종이 최소 0.91) — 다른 날의 다른 종이는 0.84 까지 올라간다(같은 사람이 같은 차로 같은 칸에 쓴다).
  합성 글씨는 같은 날 모든 일보의 머리 칸이 화소까지 같아 서명에서 **표 밖 필드도 지운다** (ADR 0020).
- 양면 스캔의 빈 뒷면은 `unknown_form` 이 된다. 어두운 화소 비율로 갈린다 (실제 쪽 최소 0.046, 흰 종이·티·그림자 ≤ 0.011) — 양식을 못 찾은 쪽에서만 본다.
- 문서 하나를 한 트랜잭션으로 처리하면 30쪽에 30–70초 동안 화면의 저장이 막힌다 → 쪽마다 커밋 (1–2초).
- 접수 폴더에서 먼저 치우고 등록하면 그 사이에 끊길 때 문서가 사라진다 → 등록·커밋한 뒤에 치운다. OpenCV 4.9 는 잘린 JPEG 도 디코딩한다
  → JPEG 는 끝 표시(FF D9)까지 있어야 다 쓰인 것이다.

## 하지 말 것

- 범용 표 인식 모델로 셀을 찾으려 하지 않는다. 양식은 고정이고 정합이 더 정확하다 (ADR 0001).
- 인쇄된 머리글 값을 사실로 믿는 조인을 만들지 않는다 (ADR 0004).
- 교차검증 불일치를 자동으로 "맞춰" 넣지 않는다. 보여 주고 검수로 보낸다 (ADR 0006).
- 합성 데이터의 수치로 한글 손글씨 인식률을 말하지 않는다. 인식률은 실데이터 평가셋으로만 말한다.
- 외부 API 를 부르는 인식·교정 백엔드를 만들지 않는다. 현장은 외부 네트워크 없이 운용한다 (ADR 0003).
- 인쇄 층을 정합·분류의 기준 이미지나 크롭에 쓰지 않는다 (ADR 0017). 같은 날 섞여 쓰이는 판을 표마다의 2차 정합(괘선 스냅)으로 풀지 않는다 —
  판 B 는 줄 간격까지 달라 평행 이동으로 풀리지 않고, 모든 양식의 수치가 바뀐다. 판은 템플릿이다 (ADR 0018).
- 날짜 없는 쪽을 적재하지 않는다. 손으로 쓴 월·일이나 파일의 시각으로 날짜를 정하지 않는다 (ADR 0019). `run --date` 를 만들지 않는다.
- 다시 스캔 의심을 기계가 스스로 버리거나 합치지 않는다. 사람이 정할 때까지 적재하지 않는다 (ADR 0020).
