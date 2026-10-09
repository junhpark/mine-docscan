"""엑셀에 보이는 한글 — 머리글·상태·표시를 한 곳에 (tasks/0008 4.5). 시트마다 흩어 적지 않는다.

칸의 표시는 색에만 기대지 않는다 — 글자 표시와 바탕색을 같이 쓴다 (4.2). 범례(LEGEND)는 요약 시트에 그대로 실린다.
"""
from __future__ import annotations

# ── 칸의 상태 (4.2) — 모델의 style 과 엑셀의 글자 표시 ─────────────────────────
PENDING_MARK = "?"                 # 검수 대기: 값을 적지 않는다
ILLEGIBLE_MARK = "판독 불가"        # 사람이 보았고 읽지 못했다 (검수의 illegible)
CHECK_MARK = "✓"                   # ✓ 칸에 표시가 있다
PRESENT_MARK = "●"                 # 글씨가 있다 (서명처럼 유무만 적는 칸, 읽지 않는 작업 표의 칸)
NO_DOC_MARK = "–"                  # 운반 표: 그 날짜·자리의 일보가 없다 (자리 미정인 일보도 없다)
UNKNOWN_MARK = "–?"                # 운반 표: 그 날짜·자리의 일보가 없는데 자리 미정인 일보가 있다
OVERLAP_MARK = "겹침"              # 운반 표: 한 일보 쪽에 같은 광종·편·주야의 행이 둘 이상 — 더하지 않는다 (tasks/0009 4.1 가)
PROVISIONAL = " (잠정)"             # 판정·결과에 확정되지 않은 값이 끼어 있다
UNRESOLVED_SLOT = "자리 미정"        # 일보의 자리를 모른다 (교차검증이 자리를 정하지 못한 쪽)

# 업무 행의 상태 열 — 값 열이 확정인가 (4.2 의 둘째 표)
ROW_STATE = {"value": "확정", "empty": "빈 칸", "pending": "검수 대기", "illegible": "판독 불가"}

LEGEND = [
    ("value", "12", "확정된 값 (기계가 자동 적재했거나 사람이 검수한 값)"),
    ("printed", "ORE", "양식에 인쇄된 값 (템플릿이 확정한 값)"),
    ("meta", "V-101", "쪽의 메타 — 차량번호·작성자 … (검수·결정·라벨·파일명, 또는 자동 적재된 기계 값)"),
    ("pending", PENDING_MARK, "검수 대기 — 값을 싣지 않는다. 검수 화면에서 넣으면 다음 내보내기에 값이 된다"),
    ("illegible", ILLEGIBLE_MARK, "사람이 보았고 읽지 못했다"),
    ("mark", CHECK_MARK, "✓ 칸에 표시가 있다"),
    ("present", PRESENT_MARK, "글씨가 있다 (값을 읽지 않는 칸 — 서명, 작업 표)"),
    ("empty", "", "빈 칸 (확정)"),
    ("no_doc", NO_DOC_MARK, "운반 표: 그 날짜·자리의 일보가 없다"),
    ("unknown", UNKNOWN_MARK, "운반 표: 그 날짜·자리의 일보가 없는데 자리 미정인 일보가 있다 (그 쪽이 이 자리의 것일 수 있다)"),
    ("mismatch", "7", "교차검증 불일치 — 일보의 값을 그대로 적고 표시만 한다 (고치지 않는다)"),
    ("value", f"불일치{PROVISIONAL}", "확정되지 않은 값이 끼어 있는 판정 — 검수가 끝나면 바뀔 수 있다"),
]

# 겹침이 있는 월별 파일에만 범례에 더한다 (겹침이 없는 파일의 모델은 그대로 — tasks/0009 4.1 가)
LEGEND_OVERLAP = ("overlap", OVERLAP_MARK, "운반 표: 한 일보 쪽에 같은 광종·편·주야의 행이 둘 이상이다 — 더하지 않고 그 줄의 합계를 "
                  "비운다 (템플릿을 고친다 — minedocscan template check)")

COPY_NOTE = "이 파일은 DB 의 사본입니다 — 고치려면 검수 화면에서 고칩니다 (엑셀을 고쳐도 DB 로 돌아가지 않습니다)."
NO_DATE_NOTE = "날짜를 정할 문서·실패한 문서는 이 파일에 없습니다 — 운영 화면(홈)에서 봅니다."
WAITING_NOTE = "처리 중이거나 다시 처리를 기다리는 문서가 이 날짜에 걸쳐 있습니다 — 끝난 뒤 다시 보십시오."

# ── 요약 시트 ─────────────────────────────────────────────────────────────
SUMMARY = "요약"
SUMMARY_HEAD = ["구분", "항목", "수"]
SEC_DATE, SEC_MONTH = "날짜", "달"
SEC_STAMP_TIME, SEC_STAMP_VERSION = "만든 시각", "프로그램"
SEC_PAGES, SEC_FORMS, SEC_PENDING = "쪽 — 상태", "적재된 쪽 — 양식", "검수 대기 칸"
SEC_XCHECK, SEC_XUSAGE, SEC_WAITING = "교차검증(일보↔행렬)", "계기 검산", "처리 중·다시 처리 대기 문서"
TOTAL = "합계"
PAGE_STATUS = {"loaded": "적재", "duplicate": "다시 스캔 의심", "discarded": "버림", "blank": "빈 쪽", "unknown_form": "양식 없음",
               "align_failed": "정합 실패", "classified_only": "분류만", "error": "오류"}
XCHECK_STATUS = {"match": "일치", "mismatch": "불일치", "missing_log": "일보 없음", "missing_matrix": "행렬 없음"}
CHECK_KIND = {"total": "총 = 종료 − 시작", "subtotal": "소계 = 합", "continuity": "계기의 연속성"}
CHECK_RESULT = {"match": "맞음", "mismatch": "어긋남", "gap": "빈 구간", "overlap": "겹침", "first": "첫 기록", "unknown": "모름"}
SHIFT = {"day": "주간", "night": "야간"}
READING_KIND = {"meter": "계기 값", "clock": "시각", "mixed": "섞임", "empty": "빈 칸", "pending": "모르는 칸", "none": "계기 표 없음"}
HOURS_BASIS = {"meter": "계기(종료 − 시작)", "total": "총 칸", "clock": "시각", "shifts": "근무 시각"}
ABNORMAL = {1: "유", 0: "무", None: "판정 불가"}
MATCHED_BY = {"operator": "작성자", "vehicle": "차량번호"}

# ── 양식 시트의 쪽 블록 ───────────────────────────────────────────────────
PAGE_HEAD = "쪽"
PAGE_PENDING = "검수 대기 칸"
FIELDS_HEAD = "표 밖 필드"
ROW_HEAD = "행"

# ── 업무 시트 (시트 이름과 열) — 끝의 열은 TAIL ─────────────────────────────
STATE, MACHINE, SOURCE, FIELD_ID = "상태", "기계 값(확정 아님)", "출처", "필드 ID"

SHEETS = {
    "haul_log": "운반(일보)", "haul_matrix": "운반(행렬)", "xcheck_haul": "교차검증", "assignment": "배차",
    "usage": "가동 기록", "tally": "작업량", "xcheck_usage": "계기 검산", "inspection": "점검",
    "haul_table": "운반 표",
}
COLUMNS = {
    "haul_log": ["날짜", "자리", "차량번호", "작성자", "광종", "편", "주야", "횟수"],
    "haul_matrix": ["날짜", "자리", "머리글 차량번호", "머리글 작성자", "광종", "편", "횟수"],
    "xcheck_haul": ["날짜", "자리", "차량번호", "작성자", "광종", "편", "일보 횟수", "행렬 횟수", "판정"],
    "assignment": ["날짜", "자리", "차량번호", "작성자", "머리글 차량번호", "머리글 작성자", "맞춘 기준", "머리글과 다름"],
    "usage": ["날짜", "양식", "장비", "작성자", "계기 칸", "계기 시작", "계기 종료", "계기 총", "시각 시작", "시각 종료",
              "근무 시각", "근무 분", "작업 줄", "서명", "가동 시간", "근거"],
    "tally": ["날짜", "양식", "장비", "항목", "장소", "열", "주야", "소계", "수"],
    "xcheck_usage": ["날짜", "종류", "장비", "값 A", "값 B", "차이", "낀 날", "결과", "비교한 쪽"],
    "inspection": ["날짜", "장비", "이상", "점검내역"],
}
SUBTOTAL_MARK = "소계"
YES = "예"

# ── 월별 파일 ───────────────────────────────────────────────────────────────
MONTH_DAYS = "날짜별"
MONTH_DAY_COLUMNS = ["날짜", "쪽", "적재되지 않은 쪽", "검수 대기 칸", "교차검증 일치", "교차검증 불일치", "교차검증 한쪽 없음",
                     "계기 검산 어긋남", "자리 미정 일보 쪽"]
HAUL_TABLE_HEAD = ["날짜", "주야"]
HAUL_TABLE_SUM = "합계"
LONG = {"haul": "긴 표 — 운반", "xcheck_haul": "긴 표 — 교차검증", "usage": "긴 표 — 가동 기록", "tally": "긴 표 — 작업량",
        "xcheck_usage": "긴 표 — 계기 검산", "inspection": "긴 표 — 점검", "assignment": "배차"}
HAUL_LONG_COLUMNS = ["날짜", "역할", "자리", "차량번호", "작성자", "광종", "편", "주야", "횟수"]
ROLE = {"log": "일보", "matrix": "행렬"}
