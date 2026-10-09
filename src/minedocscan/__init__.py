"""minedocscan — 광산 현장 수기 문서 스캔 → 인식 → 데이터베이스 파이프라인.

단계: ingest → classify → align → extract → recognize → correct → validate → load → crosscheck
구조와 설계 원칙은 docs/ARCHITECTURE.md, 작업 규칙은 CLAUDE.md 를 본다.
"""

__version__ = "1.0.0"
