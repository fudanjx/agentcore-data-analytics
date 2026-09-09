---
name: ah-analytics-urgentcarecenter
description: Column reference and SQL guidance for the ah-analytics urgentcarecenter table (Combined_UCC — Urgent Care Centre / A&E attendances). Use when writing SQL against the urgentcarecenter table, or when the user asks about emergency attendances, triage acuity, ED waiting times, UCC case end disposition, or A&E volume at Alexandra Hospital.
---

# AH Analytics — urgentcarecenter table (UCC / A&E)

**One row per attendance. Primary date: `Visit_Date`.**

## Query baseline

Use the `urgentcarecenter` filters and canonical date in `references/data-ontology.yaml`.

## Identifiers

`Case_No` is populated broadly across both eras (~93% of rows, including most NGEMR-era rows) — don't use its presence/absence to detect era. `PAT_ENC_CSN_ID` is the clean era switch: populated only for NGEMR-era rows, always null for SAP-era rows — use it as the primary identifier for NGEMR-era attendances and for the admission join. `SAP_IP_CASE_NO`, despite the name, is **not** a legacy-era field — it's null for every SAP-era row and populated for only ~19% of NGEMR-era rows (attendances that resulted in an inpatient admission). Per `data-ontology.yaml`'s `do_not_join`, it does not match `admission.case_no` on live data — don't join on it.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Visit_Date` | TIMESTAMP | Date of attendance — primary date filter |
| `Visit_Time` | TIME | Arrival/registration time |
| `Case_End_Type` | TEXT | Discharge disposition — see full mapping below |
| `CONSULT_ACUITY` | TEXT | Current production acuity field (see below) |
| `TRIAGE_ACUITY` | TEXT | Triage-stage acuity — still populated but no longer the primary reporting field, see below |
| `PACS` | TEXT | Most complete acuity field (~99.8% populated) — the ultimate fallback for both of the above |
| `Arrival_Mode` | TEXT | `Walk In` (~97% of rows), `Police Vehicle`, `Private Ambulance`, `993 Ambulance`, `SCDF Ambulance`, `Ambulance (Others)` |
| `Att_Phy_Name` | TEXT | Attending physician name |
| `Att_Phy_MCR_No` | TEXT | Attending physician MCR |
| `Pri_Diag_Code` | TEXT | Primary diagnosis ICD code |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter identifier — see identifiers note above. |
| `SAP_IP_CASE_NO` | TEXT | See identifiers note above — do not join it to live `admission.case_no`. |
| `Gender` | TEXT | `Male` / `Female` |
| `PAT_AGE` | NUMERIC | Age at visit |
| `Trauma` | TEXT | `Non-Trauma/Emergency` (majority), `Trauma/Emergency`, `Non-Trauma/Non-Emergency`, `Trauma/Non-Emergency` |
| `RESIDENCY` | TEXT | `SG`, `FR`, `PR`, `FNR`, `Others` — NGEMR-only, null for SAP-era rows |
| `EVENT_ARRIVAL_TIME` | TIMESTAMP | Actual arrival timestamp |
| `TRIAGE_START_TIME` | TIMESTAMP | Triage start |
| `TRIAGE_END_TIME` | TIMESTAMP | Triage end |
| `HOSPITAL_ADMISSION_DTTM` | TIMESTAMP | Time admitted to inpatient (if admitted) |
| `IP_BED_REQUEST_TIME` | TIMESTAMP | When inpatient bed requested |
| `IP_ADMIT_TIME` | TIMESTAMP | When inpatient bed assigned |
| `ED_DEPARTURE_DTTM` | TIMESTAMP | Time patient left ED |
| `ED_LODGER_FLAG` | TEXT | `Y` / `N` / null — flags whether the patient is an ED lodger (boarding in ED, typically awaiting an inpatient bed) |
| `cnt` | INTEGER | Always 1 |

## Acuity — resolve in priority order

### ⚠️ Production changed its primary acuity field on 2026-07-01

Production pivots now use **`CONSULT_ACUITY`**, not `TRIAGE_ACUITY` (changed from `TRIAGE_ACUITY` on 2026-07-01). `CONSULT_ACUITY` is only backfilled from `PACS` (never from `TRIAGE_ACUITY`), so the current production-consistent acuity is:

```sql
COALESCE(NULLIF("CONSULT_ACUITY", ''), "PACS") AS acuity
```

`TRIAGE_ACUITY` is a separate field, still real and still filled with its own fallback chain (`TRIAGE_ACUITY` → `CONSULT_ACUITY` → `PACS`) if you specifically need the triage-stage acuity rather than the consult-stage one used in current reports:

```sql
COALESCE(NULLIF("TRIAGE_ACUITY", ''), NULLIF("CONSULT_ACUITY", ''), "PACS") AS triage_acuity
```

Both `TRIAGE_ACUITY` and `CONSULT_ACUITY` are null for roughly half of rows before this fallback; `PACS` alone is populated for ~99.8% of rows.

## Case_End_Type — full mapping

Production only relabels these specific raw values; **everything else passes through unchanged** — the raw feed has many more distinct values than the relabeled set, and most of them are never touched:

| Raw value | Relabeled to |
|---|---|
| `Admitted` | `Admit` |
| `Admit to Other Hosp` | `Decant` |
| `Patient discharged` | `Discharged` |
| `Dis. against Advice` | `AMA/AOR` |
| `Follow-up at SOC` | `Direct to Subspecialty` |
| `Trans followup at ano A&E` | `Transfer to Other ED` |
| `Dis to Community Hosp` | `Discharge to Community Hosp` |
| `Death Non-Coroner` | `Death Non-Coroners` |
| `Death Coroner` | `Death Coroners` |

```sql
CASE "Case_End_Type"
  WHEN 'Admitted'                   THEN 'Admit'
  WHEN 'Admit to Other Hosp'        THEN 'Decant'
  WHEN 'Patient discharged'         THEN 'Discharged'
  WHEN 'Dis. against Advice'        THEN 'AMA/AOR'
  WHEN 'Follow-up at SOC'           THEN 'Direct to Subspecialty'
  WHEN 'Trans followup at ano A&E'  THEN 'Transfer to Other ED'
  WHEN 'Dis to Community Hosp'      THEN 'Discharge to Community Hosp'
  WHEN 'Death Non-Coroner'          THEN 'Death Non-Coroners'
  WHEN 'Death Coroner'              THEN 'Death Coroners'
  ELSE "Case_End_Type"
END AS case_end_type
```

**Complete raw-value list (confirmed) — everything below passes through the `ELSE` unchanged:**

Passthrough values with no relabeling rule: `Trans. to NUHS hosp`, `Discharge to Police`, `Follow-up at PHC`, `Follow-up at GP`, `Trans. to NHG Hospital`, `Trans. to Singhealth Hosp`, `Others`, `Absconded`, `Trans. to private Hosp`, `Discharge to Nursing Home`, `Discharge to Prison`, `13 Months Auto Case End`, `Auto Case End (A&E)`, `Admission No-Show`, `Discharge to DRC`, `Death on Arrival NCoroner`, `Admit to EDTC/EDTU`, `Admit to DS`.

Raw values that already match a relabeled target (land on the same value the mapping table would produce anyway): `Decant`, `Discharged`, `Admit`, `Transfer to Other ED`, `Direct to Subspecialty`, `AMA/AOR`, `Discharge to Community Hosp`, `Death Non-Coroners`, `Death Coroners`.

`Case_End_Type` can also be null. Group the passthrough values deliberately if the user needs a clean disposition breakdown.

`Cancelled` rows are removed entirely before this mapping runs (see the standard filter in `data-ontology.yaml`), and never appear in the final `Case_End_Type`.

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
  COALESCE(NULLIF("CONSULT_ACUITY",''), "PACS") AS acuity,
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

## Open items

See `references/urgentcarecenter-open-questions.md`.
