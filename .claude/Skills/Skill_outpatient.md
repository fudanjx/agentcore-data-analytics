---
name: ah-analytics-outpatient
description: Column reference and SQL guidance for the ah-analytics outpatient table (Combined_SOC — Specialist Outpatient Clinic visits). Use when writing SQL against the outpatient table, or when the user asks about SOC visits, clinic appointments, new vs repeat patients, first visit rates, or telehealth attendance at Alexandra Hospital.
---

# AH Analytics — outpatient table (SOC visits)

**One row per appointment. Primary date: `Visit_Date`.**

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  -- AND "APPT_STATUS" NOT IN ('Booked', 'Cancelled')
  STATUS IN ("A")
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND "Trt_Cat" != 'NC'
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Visit_Date` | TIMESTAMP | Date visit occurred — primary date filter |
| `Visit_Time` | TIME | Actual visit time |
| `APPT_TIME` | TIME | Scheduled appointment time |
| `Visit_Type` | TEXT | Visit classification — see mapping below |
| `APPT_STATUS` | TEXT | Lifecycle status (`Completed`, `Cancelled`, `Booked`) |
| `Trt_Cat` | TEXT | Treatment category; `NC` = non-consult (exclude) |
| `Class` | TEXT | Patient class: `SUB`=subsidised, `A`/`B1`=paying |
| `Clinical_Dept` | TEXT | Department name |
| `Sub-Specialty` | TEXT | Sub-specialty (hyphen in name — always double-quote) |
| `Trt_OU` | TEXT | Clinic name |
| `Attn_Phy` | TEXT | Attending physician name |
| `Attn_MCR` | TEXT | Attending physician MCR number |
| `Age` | TEXT | Patient age — cast to INT for ranges |
| `Sex` | TEXT | `M` / `F` |
| `Referral_type` | TEXT | How patient was referred |
| `Pri_Diag_Code` | TEXT | ICD-10 diagnosis code |
| `PAT_ENC_CSN_ID` | TEXT | Join key to `procedure` table |
| `cnt` | INTEGER | Always 1 — use `COUNT(*)` or `SUM("cnt")` |

## Visit_Type codes

| Code | New/Repeat | Mode |
|------|-----------|------|
| `FV` | First Visit | In-person |
| `RV` | Repeat Visit | In-person |
| `FW` | First Visit | Walk-in |
| `RW` | Repeat Visit | Walk-in |
| `DF` | First Visit | Telehealth |
| `DR` | Repeat Visit | Telehealth |
| `FD` | First Visit | Telehealth (alt) |
| `RD` | Repeat Visit | Telehealth (alt) |

```sql
-- New visits only
WHERE "Visit_Type" IN ('FV','FW','DF','FD')

-- Repeat visits only
WHERE "Visit_Type" IN ('RV','RW','DR','RD')

-- Telehealth only
WHERE "Visit_Type" IN ('DF','DR','FD','RD')
```

## Patient class SQL

```sql
CASE
  WHEN "Class" IN ('A','B1') THEN 'Paying'
  WHEN "Class" IN ('SUB','B2','C') THEN 'Subsidised'
  ELSE "Class"
END AS pat_class
```

## Example: monthly new vs repeat trend

```sql
SELECT
  DATE_TRUNC('month', "Visit_Date") AS month,
  SUM(CASE WHEN "Visit_Type" IN ('FV','FW','DF','FD') THEN 1 ELSE 0 END) AS new_visits,
  SUM(CASE WHEN "Visit_Type" IN ('RV','RW','DR','RD') THEN 1 ELSE 0 END) AS repeat_visits
FROM outpatient
WHERE "prelim_flag" = 'N'
  AND "APPT_STATUS" NOT IN ('Booked','Cancelled')
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND "Trt_Cat" != 'NC'
  AND "Visit_Date" >= '2024-01-01'
GROUP BY 1 ORDER BY 1;
```

## Join to procedure

```sql
FROM outpatient o
JOIN procedure p ON o."PAT_ENC_CSN_ID" = p."PAT_ENC_CSN_ID"
```
