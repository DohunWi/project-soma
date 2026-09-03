#!/usr/bin/env python3
"""
db/inspect.py
─────────────
실제 DB 의 테이블·컬럼을 찍어보고, 코드가 기대하는 것과 대조합니다.

마이그레이션을 돌리기 전/후에 확인하세요.
INSERT 가 조용히 실패하면 시연 내내 적재가 0건인 채로 끝납니다.

    python db/inspect.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 가 없습니다.  pip install -r db/requirements.txt")

# server/db_writer.py 가 실제로 INSERT 하는 컬럼
EXPECTED = {
    "sensor_logs": ["t", "user_name", "raw_data", "state", "score", "balance",
                    "blink_rate", "face_distance_cm", "static_hold_sec"],
    "blink_events": ["t", "user_name"],
    "reports": ["user_name", "kind", "start_t", "end_t", "summary"],
}


def main():
    if not os.getenv("DB_HOST"):
        sys.exit(".env 의 DB_* 가 비어 있습니다")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "postgres"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), connect_timeout=5)
    cur = conn.cursor()

    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' ORDER BY table_name""")
    tables = [r[0] for r in cur.fetchall()]
    print(f"public 스키마 테이블: {', '.join(tables) or '(없음)'}\n")

    ok = True
    for table, cols in EXPECTED.items():
        if table not in tables:
            print(f"✗ {table} — 테이블 없음")
            ok = False
            continue

        cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_name=%s ORDER BY ordinal_position""", (table,))
        actual = {r[0]: r[1] for r in cur.fetchall()}
        missing = [c for c in cols if c not in actual]
        extra = [c for c in actual if c not in cols and c not in ("id", "created_at")]

        mark = "✓" if not missing else "✗"
        print(f"{mark} {table}")
        for c, t in actual.items():
            tag = "" if c in cols or c in ("id", "created_at") else "   (코드가 안 씀)"
            print(f"    {c:20} {t}{tag}")
        if missing:
            print(f"    ⚠ 없는 컬럼: {', '.join(missing)}")
            ok = False
        if extra:
            print(f"    · 구 컬럼: {', '.join(extra)}")

        cur.execute(f"SELECT count(*) FROM {table}")
        print(f"    행 {cur.fetchone()[0]}개\n")

    if not ok:
        print("→ 마이그레이션이 필요합니다:")
        print("   psql \"$DATABASE_URL\" -f db/migrations/001_reconcile.sql")
        print("   또는 Supabase 대시보드 SQL Editor 에 붙여넣기")
    else:
        print("→ 코드가 기대하는 컬럼이 모두 있습니다.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
