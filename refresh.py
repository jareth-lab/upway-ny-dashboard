"""
NY Facility Dashboard — Metabase → data.json
Run by GitHub Actions every 30 min during business hours.
Credentials come from env vars METABASE_USER and METABASE_PASS.
"""
import os
import json
import requests
from datetime import datetime, timezone

METABASE_URL = "https://metabase.upway.app"
DB = 6

# ── SQL queries ────────────────────────────────────────────────────────────────

SQL_A = """
WITH ny_bikes AS (
  SELECT bk.id AS bike_id FROM `upway-331113.prod_public.Bike` bk
  JOIN `upway-331113.prod_public.PhysicalBike` pb ON bk.physicalBikeId = pb.id
  WHERE pb.warehouse='newyork'
),
days AS (
  SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 13 DAY),
    CURRENT_DATE('America/New_York'))) AS day
),
ev AS (
  SELECT u.itemId, u.date, u.name,
    JSON_EXTRACT_SCALAR(u.details, '$.step') AS step,
    JSON_EXTRACT_SCALAR(u.details, '$.physicalBike.location') AS loc
  FROM `upway-331113.prod_public.Update` u
  JOIN ny_bikes nb ON u.itemId = nb.bike_id
  WHERE u.itemType='bike'
    AND u.date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 DAY)
),
checkins AS (
  SELECT DATE(c.endedAt, 'America/New_York') d, COUNT(*) n
  FROM `upway-331113.prod_public.BikeCheckinSession` c
  JOIN ny_bikes nb ON CAST(c.bikeId AS STRING) = nb.bike_id
  WHERE c.endedAt >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 DAY)
  GROUP BY 1
),
daily_ev AS (
  SELECT DATE(date, 'America/New_York') d,
    COUNTIF(name='imagesUploaded') photos,
    COUNTIF(name='toPrepareForUpload') upload_list,
    COUNTIF(name='repairSessionFinished') repairs,
    COUNT(DISTINCT IF(name='checkoutStepSucceeded' AND step='packaging', itemId, NULL)) shipments
  FROM ev
  WHERE date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 DAY)
  GROUP BY 1
),
seq AS (
  SELECT itemId, date, loc,
    LAG(loc) OVER (PARTITION BY itemId ORDER BY date) prev_loc
  FROM ev WHERE loc IS NOT NULL
),
rz AS (
  SELECT DATE(date, 'America/New_York') d,
    COUNTIF(loc='Redzone' AND (prev_loc IS NULL OR prev_loc!='Redzone')) rz_in,
    COUNTIF(prev_loc='Redzone' AND loc!='Redzone') rz_out
  FROM seq
  WHERE date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 DAY)
  GROUP BY 1
)
SELECT FORMAT_DATE('%Y-%m-%d', days.day) AS day,
  COALESCE(checkins.n,0) checkins,
  COALESCE(daily_ev.photos,0) photos,
  COALESCE(daily_ev.upload_list,0) upload_list,
  COALESCE(daily_ev.repairs,0) repairs,
  COALESCE(daily_ev.shipments,0) shipments,
  COALESCE(rz.rz_in,0) redzone_in,
  COALESCE(rz.rz_out,0) redzone_out
FROM days
LEFT JOIN checkins ON days.day = checkins.d
LEFT JOIN daily_ev ON days.day = daily_ev.d
LEFT JOIN rz ON days.day = rz.d
ORDER BY days.day
"""

SQL_B = """
SELECT u.initiator, COUNT(*) sessions
FROM `upway-331113.prod_public.Update` u
JOIN `upway-331113.prod_public.Bike` bk ON u.itemId = bk.id
JOIN `upway-331113.prod_public.PhysicalBike` pb ON bk.physicalBikeId = pb.id
WHERE u.itemType='bike' AND u.name='repairSessionFinished'
  AND pb.warehouse='newyork'
  AND u.initiator IS NOT NULL
  AND u.date >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE('America/New_York'), WEEK(MONDAY)), 'America/New_York')
GROUP BY 1 ORDER BY sessions DESC LIMIT 8
"""

SQL_C = """
WITH ny_bikes AS (
  SELECT bk.id AS bike_id FROM `upway-331113.prod_public.Bike` bk
  JOIN `upway-331113.prod_public.PhysicalBike` pb ON bk.physicalBikeId = pb.id
  WHERE pb.warehouse='newyork'
),
params AS (
  SELECT CURRENT_DATE('America/New_York') AS today,
    DATE_TRUNC(CURRENT_DATE('America/New_York'), MONTH) AS m_start,
    LAST_DAY(CURRENT_DATE('America/New_York')) AS m_end
),
tgt AS (
  SELECT p.*,
    CASE EXTRACT(MONTH FROM p.today)
      WHEN 6 THEN 520.5 WHEN 7 THEN 580 WHEN 8 THEN 637.5
      WHEN 9 THEN 637.5 WHEN 10 THEN 580 WHEN 11 THEN 524
      WHEN 12 THEN 439 ELSE 520 END AS t_flow,
    CASE EXTRACT(MONTH FROM p.today)
      WHEN 6 THEN 525.5 WHEN 7 THEN 549 WHEN 8 THEN 597
      WHEN 9 THEN 501.5 WHEN 10 THEN 477.5 WHEN 11 THEN 525.5
      WHEN 12 THEN 477.5 ELSE 520 END AS t_ship
  FROM params p
),
dleft AS (
  SELECT COUNT(*) AS n FROM tgt t,
    UNNEST(GENERATE_DATE_ARRAY(t.today, t.m_end)) d
  WHERE EXTRACT(DAYOFWEEK FROM d) NOT IN (1, 7)
    AND d NOT IN ('2026-06-19','2026-07-03','2026-09-07','2026-11-11','2026-11-26','2026-12-25')
),
dtotal AS (
  SELECT COUNT(*) AS n FROM tgt t,
    UNNEST(GENERATE_DATE_ARRAY(t.m_start, t.m_end)) d
  WHERE EXTRACT(DAYOFWEEK FROM d) NOT IN (1, 7)
    AND d NOT IN ('2026-06-19','2026-07-03','2026-09-07','2026-11-11','2026-11-26','2026-12-25')
),
chk AS (
  SELECT
    COUNTIF(DATE(c.endedAt,'America/New_York')=t.today) today_n,
    COUNTIF(DATE(c.endedAt,'America/New_York')<t.today) mtd
  FROM `upway-331113.prod_public.BikeCheckinSession` c
  JOIN ny_bikes nb ON CAST(c.bikeId AS STRING)=nb.bike_id
  CROSS JOIN tgt t
  WHERE DATE(c.endedAt,'America/New_York') >= t.m_start
),
ev AS (
  SELECT u.itemId, u.name,
    JSON_EXTRACT_SCALAR(u.details,'$.step') AS step,
    DATE(u.date,'America/New_York') AS d
  FROM `upway-331113.prod_public.Update` u
  JOIN ny_bikes nb ON u.itemId=nb.bike_id
  CROSS JOIN tgt t
  WHERE u.itemType='bike' AND DATE(u.date,'America/New_York') >= t.m_start
),
upl AS (
  SELECT COUNTIF(d=t.today) today_n, COUNTIF(d<t.today) mtd
  FROM ev CROSS JOIN tgt t WHERE name='toPrepareForUpload'
),
rep AS (
  SELECT COUNTIF(d=t.today) today_n, COUNTIF(d<t.today) mtd
  FROM ev CROSS JOIN tgt t WHERE name='repairSessionFinished'
),
shp_days AS (
  SELECT d, COUNT(DISTINCT itemId) n FROM ev
  WHERE name='checkoutStepSucceeded' AND step='packaging' GROUP BY d
),
shp AS (
  SELECT
    COALESCE(SUM(IF(d=t.today,n,0)),0) today_n,
    COALESCE(SUM(IF(d<t.today,n,0)),0) mtd
  FROM shp_days CROSS JOIN tgt t
)
SELECT
  GREATEST(25, LEAST(
    ROUND(GREATEST(tgt.t_flow-chk.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n * 1.2)
  )) AS checkins_goal,
  ROUND(100*chk.today_n/GREATEST(GREATEST(25,LEAST(
    ROUND(GREATEST(tgt.t_flow-chk.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n*1.2))),1)) AS checkins_pct,
  GREATEST(25, LEAST(
    ROUND(GREATEST(tgt.t_flow-upl.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n * 1.2)
  )) AS uploads_goal,
  ROUND(100*upl.today_n/GREATEST(GREATEST(25,LEAST(
    ROUND(GREATEST(tgt.t_flow-upl.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n*1.2))),1)) AS uploads_pct,
  GREATEST(25, LEAST(
    ROUND(GREATEST(tgt.t_flow-rep.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n * 1.2)
  )) AS repairs_goal,
  ROUND(100*rep.today_n/GREATEST(GREATEST(25,LEAST(
    ROUND(GREATEST(tgt.t_flow-rep.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_flow/dtotal.n*1.2))),1)) AS repairs_pct,
  GREATEST(25, LEAST(
    ROUND(GREATEST(tgt.t_ship-shp.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_ship/dtotal.n * 1.2)
  )) AS shipments_goal,
  ROUND(100*shp.today_n/GREATEST(GREATEST(25,LEAST(
    ROUND(GREATEST(tgt.t_ship-shp.mtd,0)/GREATEST(dleft.n,1)),
    ROUND(tgt.t_ship/dtotal.n*1.2))),1)) AS shipments_pct
FROM chk, upl, rep, shp, tgt, dleft, dtotal
"""

# ── Metabase helpers ───────────────────────────────────────────────────────────

def get_session(user, password):
    r = requests.post(
        f"{METABASE_URL}/api/session",
        json={"username": user, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def run_query(sql, session_token):
    r = requests.post(
        f"{METABASE_URL}/api/dataset",
        headers={
            "Content-Type": "application/json",
            "X-Metabase-Session": session_token,
        },
        json={"database": DB, "native": {"query": sql}, "type": "native"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Metabase query error: {data['error']}")
    cols = [c["name"] for c in data["data"]["cols"]]
    return [dict(zip(cols, row)) for row in data["data"]["rows"]]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    user = os.environ["METABASE_USER"]
    password = os.environ["METABASE_PASS"]

    print("Authenticating to Metabase…")
    token = get_session(user, password)

    print("Running queries A, B, C…")
    rows_a = run_query(SQL_A, token)
    rows_b = run_query(SQL_B, token)
    rows_c = run_query(SQL_C, token)

    if not rows_c:
        raise RuntimeError("Goals query returned no rows")

    # Coerce numeric types (Metabase sometimes returns strings)
    for row in rows_a:
        for k in ["checkins", "photos", "upload_list", "repairs", "shipments", "redzone_in", "redzone_out"]:
            row[k] = int(row.get(k) or 0)

    for row in rows_b:
        row["sessions"] = int(row.get("sessions") or 0)

    goals = rows_c[0]
    for k in list(goals.keys()):
        goals[k] = int(goals[k] or 0)

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "daily": rows_a,
        "leaderboard": rows_b,
        "goals": goals,
    }

    with open("data.json", "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"data.json written — {len(rows_a)} daily rows, {len(rows_b)} leaderboard rows")


if __name__ == "__main__":
    main()
