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
  AND ("Trt_Cat" != 'NC' OR "Sub-Specialty_ID" IN ('LSHAPROS','LSHADEN','LSHAGDEN','LSHAGDGD'))
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `procedure` / `urgentcarecenter`. **Blank for new encounters created from Feb 2026 (NBS go-live) onward** — use `PAT_ENC_CSN_ID`. |
| `Visit_Date` | TIMESTAMP | Date visit occurred — primary date filter |
| `Visit_Time` | TIME | Actual visit time |
| `APPT_TIME` | TIME | Scheduled appointment time |
| `Visit_Type` | TEXT | Visit classification — see mapping below |
| `APPT_STATUS` | TEXT | Lifecycle status (`Completed`, `Arrived`,`Cancelled`, `Booked`, `Did Not Attend`) |
| `Status` | TEXT | Lifecycle status; `P`=Planned, `A`=Actual |
| `Trt_Cat` | TEXT | Treatment category; `NC` = non-consult (exclude, except Dental — see above) |
| `Class` | TEXT | Raw patient class code (`A`, `AP`, `ARF`, `B1`, `B1P`, `B1RF`, `B2`, `B2P`, `B2RF`, `C`, `CP`, `CRF`, `NR`, `PTE`, `PTEP`, `PTRF`, `SUB`, `SUBP`, etc.) — **do not eyeball this as already Private/Subsidised**, it must go through the `pt_class_abc` lookup below first |
| `Clinical_Dept` | TEXT | Department name |
| `Sub-Specialty` | TEXT | Sub-specialty (hyphen in name — always double-quote) |
| `Sub-Specialty_ID` | TEXT | Sub-specialty code — used to detect Dental (see above) and Psych/Cardiology I-Care re-tagging (see below) |
| `Trt_OU` | TEXT | Clinic name |
| `Attn_Phy` | TEXT | Attending physician name |
| `Attn_MCR` | TEXT | Attending physician MCR number |
| `Age` | TEXT | Patient age — cast to INT for ranges |
| `Sex` | TEXT | `M` / `F` |
| `Referral_type` | TEXT | How patient was referred |
| `Pri_Diag_Code` | TEXT | ICD-10 diagnosis code |
| `PAT_ENC_CSN_ID` | TEXT | Join key to `procedure` table. **Null before 2023-01-01** (SAP era). |
| `cnt` | INTEGER | Always 1 — use `COUNT(*)` or `SUM("cnt")` |

## Trt_OU relabeling (not previously documented)

Production renames/regroups `Trt_OU` before any pivot:
- `'AH ORTHOPAEDIC CENTRE'` → `'ALEX ORTHOPAEDIC CENTRE'` (legacy SAP name → NGEMR name)
- Psych Medicine SOC sessions → `Trt_OU = 'Psych Medicine (I-Care)'` — **identification logic changes on 1 Aug 2026**, see below
- Visits under sub-specialty `LSCHCACA` → `Trt_OU = 'Cardiology (I-Care)'`

If replicating `Monthly_Att_TrtOU` exactly, apply these relabels before grouping by `Trt_OU`.

### ⚠️ Psych Medicine identification change — effective 1 Aug 2026

Up to and including 31 Jul 2026, Psych Med SOC sessions are identified indirectly — by a fixed list of doctor `Attn_MCR` codes (`L11767F`, `L17139E`, `L05460G`) attending under sub-specialty `LSCHRO`. **From 1 Aug 2026 onward, sessions are tagged directly** with a new sub-specialty ID, `LSHAPSYM`, replacing the MCR-list lookup entirely.

```sql
CASE
  WHEN "Visit_Date" < '2026-08-01'
       AND "Attn_MCR" IN ('L11767F','L17139E','L05460G')
       AND "Sub-Specialty_ID" = 'LSCHRO'
    THEN 'Psych Medicine (I-Care)'
  WHEN "Visit_Date" >= '2026-08-01'
       AND "Sub-Specialty_ID" = 'LSHAPSYM'
    THEN 'Psych Medicine (I-Care)'
  ELSE "Trt_OU"
END AS "Trt_OU"
```

Existing pre-Aug-2026 rows stay identified by the old MCR+`LSCHRO` rule and are not retroactively retagged to `LSHAPSYM`, consistent with how the Jan 2023 and Feb 2026 data-source cutovers elsewhere in this file behave — old rows keep their old shape, only new rows follow the new rule. The date-conditioned `CASE` above is correct as-is.
ds: whether the same MCR list (`L11767F`, `L17139E`, `L05460G`) keeps handling these sessions under `LSHAPSYM`, or whether other doctors now also route through it — the rule above assumes it's the same underlying clinic, just re-tagged with a direct sub-specialty code instead of an MCR lookup.


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

## Patient class & residency lookup (`pt_class_abc`)
Go through the `pt_class_abc` lookup table *first* to get `Class_abc` (and, separately, `Class_abc_MOH`, `Resident_Type`, `Resident_MOH`), before any Private/Subsidised collapse happens. 
| Raw `Class` | `Class_abc` | `Class_abc_MOH` | `Resident_Type` | `Resident_MOH` |
|---|---|---|---|---|
| `A` | `A1` | `A1` | `SG` | `SG` |
| `AP` | `A1` | `A1` | `PR` | `PR` |
| `ARF` | `A1` | `A1` | `RF` | `FR` |
| `B1` | `B1` | `B1` | `SG` | `SG` |
| `B1P` | `B1` | `B1` | `PR` | `PR` |
| `B1RF` | `B1` | `A1` | `RF` | `FR` |
| `B2` | `B2` | `B2` | `SG` | `SG` |
| `B2P` | `B2` | `B2` | `PR` | `PR` |
| `B2RF` | `B2` | `A1` | `RF` | `FR` |
| `C` | `C` | `C` | `SG` | `SG` |
| `CP` | `C` | `C` | `PR` | `PR` |
| `CRF` | `C` | `A1` | `RF` | `FR` |
| `NR` | `A1` | `A1` | `NR` | `FNR` |
| `PTE` | `Private` | `Private` | `SG` | `SG` |
| `PTEP` | `Private` | `Private` | `PR` | `PR` |
| `PTRF` | `Private` | `Private` | `RF` | `FR` |
| `SUB` | `Subsidized` | `Subsidized` | `SG` | `SG` |
| `SUBP` | `Subsidized` | `Subsidized` | `PR` | `PR` |

Note the `_MOH` column deliberately diverges from `Class_abc` only for the `*RF` (foreigner) variants of `B1`/`B2`/`C`: `Class_abc` keeps their real bed class, but `Class_abc_MOH` reclassifies all of them up to `A1` — foreigners aren't entitled to subsidised tiers under MOH reporting rules, so for MOH statistics they're counted as full-paying `A1` regardless of the bed class they're actually occupying. Use `Class_abc` for financial/internal reporting and `Class_abc_MOH` for anything MOH-facing (e.g. F09/F04-style reports) — using the wrong one will misstate subsidised vs. private counts specifically for foreigner patients.

```sql
-- Step 1: raw Class -> Class_abc (financial/internal view)
CASE "Class"
  WHEN 'A'    THEN 'A1'     WHEN 'AP'   THEN 'A1'     WHEN 'ARF'  THEN 'A1'
  WHEN 'B1'   THEN 'B1'     WHEN 'B1P'  THEN 'B1'     WHEN 'B1RF' THEN 'B1'
  WHEN 'B2'   THEN 'B2'     WHEN 'B2P'  THEN 'B2'     WHEN 'B2RF' THEN 'B2'
  WHEN 'C'    THEN 'C'      WHEN 'CP'   THEN 'C'      WHEN 'CRF'  THEN 'C'
  WHEN 'NR'   THEN 'A1'
  WHEN 'PTE'  THEN 'Private'    WHEN 'PTEP' THEN 'Private'    WHEN 'PTRF' THEN 'Private'
  WHEN 'SUB'  THEN 'Subsidized' WHEN 'SUBP' THEN 'Subsidized'
  ELSE "Class"
END AS "Class_abc"

-- Step 2: raw Class -> Class_abc_MOH (MOH-facing view — *RF variants of B1/B2/C forced to A1)
CASE "Class"
  WHEN 'A'    THEN 'A1'     WHEN 'AP'   THEN 'A1'     WHEN 'ARF'  THEN 'A1'
  WHEN 'B1'   THEN 'B1'     WHEN 'B1P'  THEN 'B1'     WHEN 'B1RF' THEN 'A1'
  WHEN 'B2'   THEN 'B2'     WHEN 'B2P'  THEN 'B2'     WHEN 'B2RF' THEN 'A1'
  WHEN 'C'    THEN 'C'      WHEN 'CP'   THEN 'C'      WHEN 'CRF'  THEN 'A1'
  WHEN 'NR'   THEN 'A1'
  WHEN 'PTE'  THEN 'Private'    WHEN 'PTEP' THEN 'Private'    WHEN 'PTRF' THEN 'Private'
  WHEN 'SUB'  THEN 'Subsidized' WHEN 'SUBP' THEN 'Subsidized'
  ELSE "Class"
END AS "Class_abc_MOH"

-- Step 3: Class_abc -> final Pat_Class, as used by outpatient's own Monthly_Att_TrtOU etc.
CASE
  WHEN "Class_abc" IN ('A1','B1','Private') THEN 'Private'
  WHEN "Class_abc" IN ('B2','C','Subsidized') THEN'Subsidized'
  ELSE "Class_abc"
END AS "Pat_Class"
```

Residency (`Resident_Type` for internal use, `Resident_MOH` for MOH-facing reports) comes from the same lookup — `RF → FR` and `NR → FNR` are the only two codes that diverge between the two columns; everything else (`SG`, `PR`) is identical in both.

## Example: monthly new vs repeat trend

```sql
SELECT
  DATE_TRUNC('month', "Visit_Date") AS month,
  SUM(CASE WHEN "Visit_Type" IN ('FV','FW','DF','FD') THEN 1 ELSE 0 END) AS new_visits,
  SUM(CASE WHEN "Visit_Type" IN ('RV','RW','DR','RD') THEN 1 ELSE 0 END) AS repeat_visits
FROM outpatient
WHERE "prelim_flag" = 'N'
  AND ("Status" != 'P' OR "Status" IS NULL)
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND ("Trt_Cat" != 'NC' OR "Sub-Specialty_ID" IN ('LSHAPROS','LSHADEN','LSHAGDEN','LSHAGDGD'))
  AND "Visit_Date" >= '2024-01-01'
GROUP BY 1 ORDER BY 1;
```

## Join to procedure

```sql
FROM outpatient o
JOIN procedure p ON o."PAT_ENC_CSN_ID" = p."PAT_ENC_CSN_ID"
```

⚠️ For dates before 2023-01-01, `PAT_ENC_CSN_ID` will be null on both sides — this join effectively only works for Epic-era (2023+) data. See `SKILL.md` for the full explanation.
