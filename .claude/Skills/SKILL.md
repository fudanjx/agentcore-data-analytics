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
  AND "APPT_STATUS" NOT IN ('Booked', 'Cancelled')
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND "Trt_Cat" != 'NC'

-- urgentcarecenter
WHERE "prelim_flag" = 'N'
  AND "Case_End_Type" != 'Cancelled'
  AND "Att_Phy_Name" != 'CANCELLATION'

-- admission
WHERE "prelim_flag" = 'N'
  AND "Adm_Status" != 'P'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')

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
urgentcarecenter ──(PAT_ENC_CSN_ID or SAP_IP_CASE_NO)──▶ admission
outpatient       ──(PAT_ENC_CSN_ID)─────────────────────▶ procedure
admission        ──(Case_No)──────────────────────────────▶ discharge   (1:1)
admission        ──(Case_No)──────────────────────────────▶ inflight    (1:many, one row per patient-day)
admission        ──(Case_No or PAT_ENC_CSN_ID)────────────▶ procedure   (1:many)
```

## Critical pitfalls

1. **`Operation_Date` in `procedure` is TEXT** — cast before date filtering: `CAST("Operation_Date" AS DATE)`
2. **`inflight` has one row per patient per day** — `SUM("cnt")` = patient-days; `COUNT(DISTINCT "Case_No")` on one date = census
3. **`procedure` has one row per procedure** — use `COUNT(DISTINCT "Case_No")` for episode counts
4. **`Accom_Category = 'OTHER'` in `inflight`** — fall back to `"Class"` column for patient class
5. **`prelim_flag = 'Y'`** — provisional data; always exclude unless user asks for it

## Sub-skills for column-level detail

For full column listings, type mappings, and SQL examples per table, read the relevant sub-skill file:
`Skill_outpatient.md`, `Skill_urgentcarecenter.md`, `Skill_admission.md`,
`Skill_discharge.md`, `Skill_inflight.md`, `Skill_procedure.md`
