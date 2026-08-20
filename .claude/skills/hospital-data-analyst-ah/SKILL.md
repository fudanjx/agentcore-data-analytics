---
name: hospital-data-analyst-ah
description: Analyze Alexandra Hospital (AH) operational data in the ah-analytics database. Use when the user asks about AH hospital data, patient statistics, or SQL queries for the ah-analytics tables — outpatient SOC visits, A&E/urgent care, inpatient admissions, discharges, bed occupancy/patient-days, or surgical procedures. Also trigger when context clearly implies an AH analytics query even without explicit mention of "AH" or "Alexandra Hospital".
---

# AH Analytics — Data Dictionary

## Pre-query workflow

Before writing any SQL:
1. Select the correct table from the routing table below.
2. Read the corresponding reference file for column details, derived fields, and SQL patterns.
3. If the query involves patient class, residency, or paying status: also read `references/pt-class-lookup.md`.
4. Apply every mandatory filter below. Never skip `prelim_flag = 'N'`.

## Table routing

| Question is about… | Table | Reference |
|---|---|---|
| SOC/clinic visits, first vs repeat, telehealth | `outpatient` | references/outpatient.md |
| A&E / urgent care, triage, ED waiting time | `urgentcarecenter` | references/urgentcarecenter.md |
| Admissions, admission volume, admission route | `admission` | references/admission.md |
| Discharges, LOS, discharge destination, mortality | `discharge` | references/discharge.md |
| Bed occupancy, patient-days, daily census | `inflight` | references/inflight.md |
| Surgery, OT utilisation, procedures | `procedure` | references/procedure.md |
| Schema overview | `_table_metadata` | — |

## Mandatory WHERE filters (apply to every query)

```sql
-- outpatient
WHERE "prelim_flag" = 'N'
  AND ("Status" != 'P' OR "Status" IS NULL)
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
urgentcarecenter ──(PAT_ENC_CSN_ID / SAP_IP_CASE_NO→Case_No)──▶ admission
outpatient       ──(PAT_ENC_CSN_ID)────────────────────────────▶ procedure  (1:many)
admission        ──(Case_No / PAT_ENC_CSN_ID)──────────────────▶ discharge  (1:1)
admission        ──(Case_No / PAT_ENC_CSN_ID)──────────────────▶ inflight   (1:many, one row per patient-day)
admission        ──(Case_No / PAT_ENC_CSN_ID)──────────────────▶ procedure  (1:many)
```

## Critical join-key era rules (all tables)

Two data-source transitions affect every join and date filter:

1. **Jan 2023 — SAP → Epic cutover.** `PAT_ENC_CSN_ID` is null for records created before 2023-01-01. Use `Case_No` (or `SAP_IP_CASE_NO` for `urgentcarecenter`) for pre-2023 data.
2. **Feb 2026 — NBS go-live.** `Case_No` is blank for new encounters created from Feb 2026 onward; pre-go-live encounters keep their `Case_No` even in current extracts.

**Net rule:** `PAT_ENC_CSN_ID` is the safer join key from Jan 2023 onward; `Case_No` is safer before Jan 2023. For date ranges spanning both eras or the Feb 2026 cutover, coalesce on both keys and sanity-check row counts on both sides.

## Critical pitfalls

1. **`Operation_Date` in `procedure` is TEXT** — always cast: `CAST("Operation_Date" AS DATE)`.
2. **`inflight` undercounts patient-days** — same-day admit-and-discharge cases never appear in the daily census snapshot. Production adds a synthetic row for each from `discharge`/`admission`. Read `references/inflight.md` before answering any patient-days question.
3. **`procedure` has one row per procedure** — use `COUNT(DISTINCT "Case_No")` for episode counts, but note production only applies this dedup for day surgery. See `references/procedure.md` for the `Case_Identifier` caveat.
4. **`Accom_Category = 'OTHER'` in `inflight`** — fall back to `Class` for patient class, then apply the ICU/HD/ISO override chain. See `references/inflight.md`.
5. **`prelim_flag = 'Y'`** — provisional data; always exclude unless explicitly requested.
