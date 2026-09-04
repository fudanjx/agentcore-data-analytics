---
name: ah-analytics-urgentcarecenter
description: Column reference and SQL guidance for the ah-analytics urgentcarecenter table (Combined_UCC — Urgent Care Centre / A&E attendances). Use when writing SQL against the urgentcarecenter table, or when the user asks about emergency attendances, triage acuity, ED waiting times, UCC case end disposition, or A&E volume at Alexandra Hospital.
---

# AH Analytics — urgentcarecenter table (UCC / A&E)

**One row per attendance. Primary date: `Visit_Date`.**

## Query baseline

Use the `urgentcarecenter` filters and canonical date in `references/data-ontology.yaml`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Visit_Date` | TIMESTAMP | Date of attendance — primary date filter |
| `Visit_Time` | TIME | Arrival/registration time |
| `Case_End_Type` | TEXT | Discharge disposition — see values below |
| `TRIAGE_ACUITY` | TEXT | Triage acuity P1–P5 (preferred field) |
| `CONSULT_ACUITY` | TEXT | Fallback acuity if `TRIAGE_ACUITY` is null |
| `PACS` | TEXT | Legacy acuity; use only if both above are null |
| `Arrival_Mode` | TEXT | `Walk In`, `Ambulance`, etc. |
| `Att_Phy_Name` | TEXT | Attending physician name |
| `Att_Phy_MCR_No` | TEXT | Attending physician MCR |
| `Pri_Diag_Code` | TEXT | Primary diagnosis ICD code |
| `PAT_ENC_CSN_ID` | TEXT | Encounter identifier; see the ontology for the validated candidate admission join. |
| `SAP_IP_CASE_NO` | TEXT | Legacy identifier; do not join it to live `admission.case_no` (the live validation found no matches). |
| `Gender` | TEXT | `Male` / `Female` |
| `PAT_AGE` | TEXT | Age at visit |
| `EVENT_ARRIVAL_TIME` | TIMESTAMP | Actual arrival timestamp |
| `TRIAGE_START_TIME` | TIMESTAMP | Triage start |
| `TRIAGE_END_TIME` | TIMESTAMP | Triage end |
| `HOSPITAL_ADMISSION_DTTM` | TIMESTAMP | Time admitted to inpatient (if admitted) |
| `IP_BED_REQUEST_TIME` | TIMESTAMP | When inpatient bed requested |
| `IP_ADMIT_TIME` | TIMESTAMP | When inpatient bed assigned |
| `ED_DEPARTURE_DTTM` | TIMESTAMP | Time patient left ED |
| `cnt` | INTEGER | Always 1 |

## Acuity — resolve in priority order

```sql
COALESCE(
  NULLIF("TRIAGE_ACUITY", ''),
  NULLIF("CONSULT_ACUITY", ''),
  "PACS"
) AS acuity
```

## Case_End_Type values

| Value | Meaning |
|-------|---------|
| `Discharged` | Sent home |
| `Admit` | Admitted to inpatient |
| `Transfer to Other ED` | Transferred to another ED |
| `Discharge to Community Hosp` | To community hospital |
| `AMA/AOR` | Against medical advice |
| `Decant` | Moved to ward for capacity management |
| `Death` | Death in ED |

## Time interval calculations

```sql
-- Door-to-triage (minutes)
EXTRACT(EPOCH FROM ("TRIAGE_START_TIME" - "EVENT_ARRIVAL_TIME")) / 60 AS door_to_triage_mins

-- ED length of stay (hours)
EXTRACT(EPOCH FROM ("ED_DEPARTURE_DTTM" - "EVENT_ARRIVAL_TIME")) / 3600 AS ed_los_hours

-- Wait for inpatient bed (minutes)
EXTRACT(EPOCH FROM ("IP_ADMIT_TIME" - "IP_BED_REQUEST_TIME")) / 60 AS bed_wait_mins
```

## Example: monthly attendance by acuity and arrival mode

```sql
SELECT
  DATE_TRUNC('month', "Visit_Date") AS month,
  COALESCE(NULLIF("TRIAGE_ACUITY",''), NULLIF("CONSULT_ACUITY",''), "PACS") AS acuity,
  "Arrival_Mode",
  COUNT(*) AS attendances
FROM urgentcarecenter
WHERE "prelim_flag" = 'N'
  AND "Case_End_Type" != 'Cancelled'
  AND "Att_Phy_Name" != 'CANCELLATION'
  AND "Visit_Date" >= '2024-01-01'
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC;
```

## Join to admission

Use the `pat_enc_csn_id` candidate join and validate its row count for the requested period. The complete join rules are in `references/data-ontology.yaml`.
