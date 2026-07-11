---
name: ah-analytics-data-dictionary
description: Routes queries to the correct ah-analytics database table (outpatient, urgentcarecenter, admission, discharge, inflight, procedure). Use when the user asks about AH hospital data, patient statistics, or any question involving the ah-analytics database. Provides table selection logic, mandatory SQL filters to apply on every query, join keys between tables, and common pitfalls.
---

# AH Analytics — Data Dictionary (Routing Guide)

## Table selection

| Question is about... | Use table | Primary date column |
|----------------------|-----------|-------------------|
| Clinic visits, SOC appointments, first vs repeat visits, telehealth | `outpatient` | `Visit_Date` |
| A&E / urgent care, triage, ED waiting time, emergency attendance | `urgentcarecenter` | `Visit_Date` |
| Admissions, admission volume, how patients were admitted | `admission` | `Adm_Date` |
| Discharges, length of stay, discharge destination, death in hospital | `discharge` | `Disch_Date` |
| Bed occupancy, patient-days, census, beds in use, occupancy rate | `inflight` | `Inflight_Date` |
| Surgery, procedures, OT utilisation, surgeons, anaesthesia | `procedure` | `Operation_Date` |
| What tables exist, schema overview | `_table_metadata` | `loaded_at` |

## Mandatory filters — apply to every query

```sql
-- outpatient
WHERE "prelim_flag" = 'N'
  WHERE ("Status" != 'P' OR "Status" IS NULL)
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND ("Trt_Cat" != 'NC' OR "Sub-Specialty_ID" IN ('LSHAPROS','LSHADEN','LSHAGDEN','LSHAGDGD'))

-- urgentcarecenter
WHERE "prelim_flag" = 'N'
  AND "Case_End_Type" != 'Cancelled'
  AND "Att_Phy_Name" != 'CANCELLATION'

-- admission
WHERE "prelim_flag" = 'N'
  AND "Adm_Status" != 'P'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND (
    CASE
      WHEN LEFT("Adm_Nrs_OU", 2) = 'LW'
           AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
        THEN "Adm_Nrs_OU"
      ELSE "Current_Ward"
    END
  ) NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')

-- discharge
WHERE "prelim_flag" = 'N'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

-- inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

-- procedure
WHERE "prelim_flag" = 'N'
```

## How tables join

```
urgentcarecenter ──(SAP_IP_CASE_NO or PAT_ENC_CSN_ID)──▶ admission
outpatient       ──(Case_No or PAT_ENC_CSN_ID)─────────▶ procedure   (1:many)    
admission        ──(Case_No or PAT_ENC_CSN_ID)─────────▶ discharge   (1:1)
admission        ──(Case_No or PAT_ENC_CSN_ID)─────────▶ inflight    (1:many, one row per patient-day)
admission        ──(Case_No or PAT_ENC_CSN_ID)─────────▶ procedure   (1:many)
```

**⚠️ Two overlapping data-source transitions affect every join key above — check both before trusting a join:**

1. **Jan 2023 SAP → Epic cutover.** `PAT_ENC_CSN_ID` is only populated for Epic-sourced (post-2023-01-01) records — it is null/blank for anything before that date. Any join or filter on `PAT_ENC_CSN_ID` spanning pre-2023 dates will silently drop those rows. Use `Case_No` (or `SAP_IP_CASE_NO` for `urgentcarecenter`) instead for pre-2023 data.
2. **Feb 2026 NBS go-live at AH.** `Case_No` is being deprecated. Confirmed behaviour: `Case_No` is blank for **new encounters created from Feb 2026 onward**, but remains populated for encounters that were already created before the go-live (even if they still appear in current extracts). So `Case_No`-based joins do **not** uniformly break for all 2026 data — only for genuinely new encounters post-go-live. For any query touching Feb 2026 onward, prefer `PAT_ENC_CSN_ID` and treat `Case_No` as unreliable/partial, not simply "gone."

Net effect: **`PAT_ENC_CSN_ID` is the safer join key from Jan 2023 onward; `Case_No` is the safer key before Jan 2023.** For date ranges spanning both eras, or spanning the Feb 2026 cutover, coalesce on both keys rather than trusting either alone, and sanity-check row counts on both sides of a join.

## Critical pitfalls

1. **`Operation_Date` in `procedure` is TEXT** — cast before date filtering: `CAST("Operation_Date" AS DATE)`
2. **`inflight` has one row per patient per day in the raw parquet, but the actual patient-days report also folds in a synthetic same-day-admission-and-discharge dataset sourced from `admission`** (patients admitted and discharged the same calendar date never appear in a daily census snapshot). Querying `inflight` alone undercounts patient-days for wards with high same-day turnover. See Skill_inflight.md.
3. **`procedure` has one row per procedure** — use `COUNT(DISTINCT "Case_No")` for episode counts, **except** production's own case-counting sometimes uses a different device-dependent identifier (`Admsn CSN` on corporate devices) rather than `Case_No` — see Skill_procedure.md before promising exact parity with `Monthly_SurgicalEpisodes`.
4. **`Accom_Category = 'OTHER'` in `inflight`** — fall back to `"Class"` column for patient class, then apply the ICU/HD/ISO override chain documented in Skill_inflight.md. 
5. **`prelim_flag = 'Y'`** — provisional data; always exclude unless user asks for it

## Sub-skills for column-level detail

For full column listings, type mappings, and SQL examples per table, read the relevant sub-skill file:
`Skill_outpatient.md`, `Skill_urgentcarecenter.md`, `Skill_admission.md`,
`Skill_discharge.md`, `Skill_inflight.md`, `Skill_procedure.md`
