#!/usr/bin/env python3
"""
db/report/generate.py
─────────────────────
30분 시연 데이터 → 보고서. (회의 항목 2)

DB 의 sensor_logs 계열 테이블을 읽어 집계하고,
docs/contracts/report.schema.json 형식으로 백엔드에 POST 합니다.

    python db/report/generate.py --table test_30m               # 화면에만
    python db/report/generate.py --table test_30m --post        # 백엔드로 전송
    python db/report/generate.py --table sensor_logs --user forward
    python db/report/generate.py --table test_30m --save        # posture_stats_30min 적재

**집계는 SQL 이 합니다.** 327행이면 파이썬으로 해도 되지만, 실사용 로그가
쌓이면 전부 가져와 처리하는 방식은 감당이 안 됩니다.

raw_data 구조 (실제 DB 기준):
    {"chair": {"pressure": [4개]},
     "vision": {"distances": [1개], "blink_count": 정수}}
"""
import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 가 없습니다.  pip install -r db/requirements.txt")

# fusion/state.py 와 같은 기준을 씁니다. 두 곳에서 다르면 리포트와 실시간이 어긋납니다.
OCCUPANCY_MIN = 100
BALANCE_DIFF  = 50
GAP_SEC       = 120     # 이보다 긴 공백은 세션 경계로 봅니다


SQL = """
WITH s AS (
  SELECT
    COALESCE(measured_at, "time")                       AS t,
    user_name,
    (raw_data->'chair'->'pressure'->>0)::int            AS fl,
    (raw_data->'chair'->'pressure'->>1)::int            AS fr,
    (raw_data->'chair'->'pressure'->>2)::int            AS bl,
    (raw_data->'chair'->'pressure'->>3)::int            AS br,
    (raw_data->'vision'->'distances'->>0)::int          AS ir,
    (raw_data->'vision'->>'blink_count')::int           AS blink_count
  FROM {table}
  WHERE raw_data ? 'chair'
    AND ({user_filter})
),
d AS (
  SELECT *,
    fl+fr+bl+br                                         AS total,
    (fl+bl) - (fr+br)                                   AS lr_diff,
    LAG(t)  OVER (PARTITION BY user_name ORDER BY t)    AS prev_t,
    LAG(fl) OVER (PARTITION BY user_name ORDER BY t)    AS p_fl,
    LAG(fr) OVER (PARTITION BY user_name ORDER BY t)    AS p_fr,
    LAG(bl) OVER (PARTITION BY user_name ORDER BY t)    AS p_bl,
    LAG(br) OVER (PARTITION BY user_name ORDER BY t)    AS p_br
  FROM s
),
e AS (
  SELECT *,
    LEAST(EXTRACT(EPOCH FROM (t - prev_t)), %(gap)s)    AS dt,
    ABS(fl-p_fl)+ABS(fr-p_fr)+ABS(bl-p_bl)+ABS(br-p_br) AS activity
  FROM d WHERE prev_t IS NOT NULL
)
SELECT
  min(t), max(t), count(*),
  SUM(CASE WHEN total >= %(occ)s THEN dt ELSE 0 END)                        AS seated_sec,
  SUM(CASE WHEN total <  %(occ)s THEN dt ELSE 0 END)                        AS absent_sec,
  SUM(CASE WHEN total >= %(occ)s AND ABS(lr_diff) > %(bal)s THEN dt ELSE 0 END) AS imbalance_sec,
  SUM(CASE WHEN total >= %(occ)s AND COALESCE(activity,999) < 25 THEN dt ELSE 0 END) AS static_sec,
  SUM(CASE WHEN total >= %(occ)s AND ir > 200 THEN dt ELSE 0 END)  AS backgap_sec,
  SUM(CASE WHEN lr_diff >  %(bal)s THEN dt ELSE 0 END)                      AS left_sec,
  SUM(CASE WHEN lr_diff < -%(bal)s THEN dt ELSE 0 END)                      AS right_sec,
  avg(NULLIF(ir, -1)), max(blink_count)
FROM e
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="test_30m")
    ap.add_argument("--user")
    ap.add_argument("--post", action="store_true", help="백엔드 /api/report 로 전송")
    ap.add_argument("--save", action="store_true", help="posture_stats_30min 에 적재")
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT',5000)}")
    args = ap.parse_args()

    if args.table not in ("sensor_logs", "temp", "test_30m"):
        sys.exit("--table 은 sensor_logs / temp / test_30m 중 하나여야 합니다")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "postgres"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode=os.getenv("DB_SSLMODE", "require"), connect_timeout=8)
    cur = conn.cursor()

    uf = "user_name = %(user)s" if args.user else "TRUE"
    cur.execute(SQL.format(table=args.table, user_filter=uf),
                {"occ": OCCUPANCY_MIN, "bal": BALANCE_DIFF,
                 "gap": GAP_SEC, "user": args.user})
    row = cur.fetchone()
    if not row or row[2] == 0:
        sys.exit(f"{args.table} 에 집계할 데이터가 없습니다")

    (t0, t1, n, seated, absent, imbal, static, backgap,
     left_s, right_s, avg_ir, max_blink) = row
    f = lambda v: round(float(v or 0), 1)

    summary = {
        "seated_sec":         f(seated),
        "static_total_sec":   f(static),
        "imbalance_sec":      f(imbal),
        "back_gap_sec":       f(backgap),   # 허리가 등받이에서 떨어진 시간
        "close_distance_sec": 0.0,          # 얼굴↔모니터. 웹캠 미연결
        "low_blink_sec":      0.0,           # blink_count 가 아직 0 — 웹캠 미연결
        "avg_blink_rate":     0.0,
        "state_seconds": {
            "NORMAL":  f(float(seated or 0) - float(imbal or 0) - float(static or 0)),
            "CAUTION": f(imbal),
            "DANGER":  f(static),
            "ABSENT":  f(absent),
        },
        "feedback_count": 0, "feedback_accepted": 0,
    }
    report = {
        "v": 1, "user_name": args.user or "all", "kind": "session",
        "period": {"start": t0.timestamp(), "end": t1.timestamp()},
        "summary": summary,
    }

    m = lambda v: f"{float(v or 0)/60:.1f}분"
    print(f"\n── {args.table} 보고서 ──")
    print(f"  기간      {t0}  ~  {t1}   ({t1-t0})")
    print(f"  샘플      {n}행")
    print(f"  착석      {m(seated)}     자리 비움 {m(absent)}")
    print(f"  좌우 편중  {m(imbal)}     (왼쪽 {m(left_s)} / 오른쪽 {m(right_s)})")
    print(f"  정적 유지  {m(static)}")
    print(f"  등받이 이격 {m(backgap)}   평균 {float(avg_ir or 0):.0f}mm")
    print(f"            (IR 은 허리↔등받이 거리입니다. 얼굴↔모니터가 아닙니다)")
    print(f"  깜빡임    최대 누적 {max_blink}  "
          f"{'← 웹캠 미연결 (전부 0)' if not max_blink else ''}")

    # ── 데이터 품질 ────────────────────────────────────────────────
    cur.execute(f"""
        SELECT stddev((raw_data->'chair'->'pressure'->>0)::int),
               stddev((raw_data->'chair'->'pressure'->>1)::int),
               stddev((raw_data->'chair'->'pressure'->>2)::int),
               stddev((raw_data->'chair'->'pressure'->>3)::int)
        FROM {args.table} WHERE raw_data ? 'chair'""")
    sds = [float(x or 0) for x in cur.fetchone()]
    dead = [n for n, sd in zip(("FL", "FR", "BL", "BR"), sds) if sd < 10]
    if dead:
        print(f"\n  ⚠ 압력 채널 {', '.join(dead)} 의 표준편차가 10 미만입니다.")
        print(f"    표준편차: " + "  ".join(f"{n}={sd:.1f}" for n, sd in
                                            zip(('FL','FR','BL','BR'), sds)))
        print("    값이 거의 변하지 않아 균형·움직임 판정에 기여하지 못합니다.")
        print("    분압 저항을 조정해 동작 범위를 중앙으로 옮기세요.")

    if args.save:
        cur.execute("""INSERT INTO posture_stats_30min
                       (user_id, target_time, total_logs, leaning_count)
                       VALUES (NULL, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (t1, n, int(float(imbal or 0))))
        conn.commit()
        print("\n  posture_stats_30min 적재 완료")

    if args.post:
        import requests
        r = requests.post(f"{args.url}/api/report", json=report, timeout=10)
        print(f"\n  POST {args.url}/api/report → {r.status_code} {r.text[:80]}")
    else:
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
