"""통합 DB 로 싣기 (tasks/0008 4.8, ADR 0021): 작업 DB(SQLite)는 그대로, 통합 DB(PostgreSQL)에는 결과의 사본을 싣는다.

- ddl.py     대상의 표 정의 — schema.sql 에서 (메모리의 SQLite 로 읽어, 형을 넓혀, 외래 키 없이)
- scopes.py  범위(문서·날짜·통째)와 지문 — 순수 함수
- core.py    대상(Target — 대상에 쓰는 곳은 여기 하나), 한 트랜잭션의 갈아 끼우기, --check, --rebuild, 연결 함수(바꿔 끼울 수 있다)
- auto.py    watch·serve 의 바퀴 끝의 싣기 (더러운 범위, 실패하면 전체 훑기, 다시 연결하는 간격)

psycopg 는 core.connect 안에서만 import 한다 — 없어도 다른 명령은 전부 돈다.
"""
