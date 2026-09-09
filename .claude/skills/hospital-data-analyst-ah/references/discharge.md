---
name: ah-analytics-discharge
description: Column reference and SQL guidance for the ah-analytics discharge table (Combined_dc — inpatient discharges). Use when writing SQL against the discharge table, or when the user asks about length of stay, discharge disposition, in-hospital mortality, discharge destination, or patient outcomes after inpatient admission at Alexandra Hospital.
---

# AH Analytics — discharge table (inpatient discharges)

**One row per discharged episode. Primary date: `Disch_Date`.**
Use this table (not `admission`) for outcome questions: LOS, death, discharge destination.

**Two source systems are combined in one file**, distinguished by admission date and by which identifier is populated:

| | Admission date | `Case_No` | `PAT_ENC_CSN_ID` |
|---|---|---|---|
| **Legacy SAP era** | before 1 Jan 2023 | populated | null |
| **NGEMR/EPIC era** | from 1 Jan 2023 | null | populated |

Columns populated in only one era are marked in the **Era** column below.

## Full column reference

`Type` is the semantic type after the casting the reporting code applies (all columns arrive as text in the raw file). "Era" marks columns only populated in one source system; blank = populated in both.

Note: several columns hold the same information as their `admission` table counterpart but under a **different name** — `Patient_ID` (not `Pat_ID`), `Ext_Pat_No` (not `Ext_Pat_ID`), `Postal` (not `Postal_Code`). Don't assume shared column names across the two tables.

### Identifiers & demographics

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `Case_No` | TEXT | SAP only | Legacy episode identifier stem. **Concatenate with `C` (below) for the full SAP case number**, same as in `admission`. | `2800348407` | High-cardinality identifier — one per episode |
| `C` | TEXT | SAP only | Final character of the full SAP case number — concatenate onto `Case_No`, don't treat as a separate field. | `H` | Single letter, A–Z |
| `Patient_ID` | TEXT | | Internal patient ID (called `Pat_ID` in `admission`). | `Z1478946` | Letter+digit or numeric identifier |
| `Ext_Pat_No` | TEXT | | **PII — Singapore NRIC/FIN** (called `Ext_Pat_ID` in `admission`). Treat as sensitive; don't surface raw values outside authorised use. | `S1234567A` (format only) | NRIC/FIN format: 1 letter + 7 digits + 1 checksum letter |
| `Nationality` | TEXT | SAP only | Nationality code. | `SG` | `SG`, `PR`, `FR`, `FNR` |
| `Age` | TEXT | | Age at discharge. | `71` | Numeric |
| `Sex` | TEXT | | | `M` | `M` for Male, `F` for Female |
| `Postal` | TEXT | | **PII** — Singapore postal code (called `Postal_Code` in `admission`). Handle per data-governance rules. | `597264` (format only) | 6-digit numeric |

### Admission details (captured on the discharge record, for the same episode)

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Adm_Date` | TIMESTAMP | Admission date. | `2024-10-24` | Date |
| `Adm_Time` | TIME | Admission time. | `08:03:00` | `HH:MM:SS` |
| `Adm_Dept_OU` | TEXT | Admitting department code — code half of the pair with `Adm_Dept_OU_Text`. Resolve via the `Subspec` sheet in `Class.xlsx`. | `LSFAMED` | Codes defined in the `Subspec` sheet of `Class.xlsx` |
| `Adm_Dept_OU_Text` | TEXT | Admitting department name — description half of the pair with `Adm_Dept_OU`. Can differ from `Dept_OU`/`Disch_Dept_OU_Text` (below) when the patient's department changed during the stay. | `Fast Medicine` | Free text |
| `Adm_Nurs_OU` | TEXT | Admitting ward code — code half of the pair with `Adm_Nurs_OU_Text`. Can differ from `Nrs_OU` (below) when the patient's ward changed during the stay. | `LW13W` | Ward codes |
| `Adm_Nurs_OU_Text` | TEXT | Admitting ward name — description half of the pair with `Adm_Nurs_OU`. | `Alex Ward 13` | Free text |
| `Adm_Bed` | TEXT | Bed code at admission. `NONE` or null both indicate no bed assigned. | `L004004` | Bed codes, or `NONE` / null |
| `Adm_Type` | TEXT | Admission route/type code — same code set as `admission.Adm_Type`. | `EM` | `DI`, `DO`, `DS`, `EL`, `EM`, `ES`, `RA`, `SD`, `SO`, `TA` |
| `Adm_Class` | TEXT | Raw patient class at admission — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`). Can differ from `Disch_Class` (below) when the patient's class changed during the stay. | `SUB` | Codes defined in `pt_class_abc` |
| `Adm_Trt_Cat` | TEXT | Treatment/acuity category at admission — resolve via the `Acuity` sheet in `Class.xlsx`. Can differ from `Trt_Cat` (below) when acuity changed during the stay. | `CL3` | Codes defined in the `Acuity` sheet of `Class.xlsx` |
| `Adm_Status` | TEXT | Admission status. Only `A` (`Actual`) observed for this table. | `A` | `A` |
| `Adm_Status_Text` | TEXT | Description half of the pair with `Adm_Status`. | `Actual` | `Actual` |
| `Adm_Physician` | TEXT | Admitting physician staff ID — code half of the pair with `Admitting_Physician_Name`. Staff PII. | `M18760G` (format only) | Staff ID |
| `Admitting_Physician_Name` | TEXT | Admitting physician name — description half of the pair with `Adm_Physician`. Staff PII — genericise in any shared examples. | `TING, YANGHAN, YOHANES` (format only) | Free text |
| `Admission_Specialty` | TEXT | NGEMR only | Admitting program/specialty. | `Fast Program` | `Fast Program`, `Chronic Program`, `Healthy Aging Program` |

### Discharge details

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Disch_Date` | TIMESTAMP | Discharge date — primary date filter. | `2024-10-24` | Date |
| `Disch_Time` | TIME | Discharge time. | `11:11:00` | `HH:MM:SS` |
| `Dept_OU` | TEXT | Discharging department code — code half of the pair with `Disch_Dept_OU_Text`. | `LSFAMED` | Same code space as `Adm_Dept_OU` |
| `Disch_Dept_OU_Text` | TEXT | Discharging department name — description half of the pair with `Dept_OU`. | `Fast Medicine` | Free text |
| `Nrs_OU` | TEXT | Discharging ward code — code half of the pair with `Disch_Nrs_OU_Text`. Apply the ward exclusion filter here (see below). | `LW4W` | Same code space as `Adm_Nurs_OU` |
| `Disch_Nrs_OU_Text` | TEXT | Discharging ward name — description half of the pair with `Nrs_OU`. Casing/spacing varies by era for the same ward (e.g. `ALEX WARD 12` vs `Alex Ward 12`) — normalise before grouping directly. | `Alex Ward 4` | Free text |
| `Disch_Bed` | TEXT | Bed code at discharge. `NONE` or null both indicate no bed assigned. | `L011017` | Bed codes, or `NONE` / null |
| `Disch_Class` | TEXT | Raw patient class at discharge — resolve through `pt_class_abc`. | `SUB` | Codes defined in `pt_class_abc` |
| `Trt_Cat` | TEXT | Treatment/acuity category at discharge — resolve via the `Acuity` sheet in `Class.xlsx`. Identical to `Discharge_Acuity_Level` (below) — use one, not both. | `CL3` | Codes defined in the `Acuity` sheet of `Class.xlsx` |
| `Discharge_Acuity_Level` | TEXT | Alias of `Trt_Cat` — always the same value. Kept for backward compatibility only. | `CL3` | Same values as `Trt_Cat` |
| `Disch_Status` | TEXT | Discharge status. Filter `Disch_Status = 'A'` for finalised discharge records — part of the standard filter set alongside `Adm_Type` and `Nrs_OU` below. | `A` | `A` |
| `Disch_Type` | TEXT | MOH discharge-type code — see canonical mapping below for consistent reporting across SAP/NGEMR eras. Same code set as `admission.Disch_Type`. | `09` | See `Disch_Type` canonical mapping below |
| `Discharge_Type_Text` | TEXT | Free-text discharge disposition paired with `Disch_Type` (called `Disch_Type_1` in `admission`). Raw text varies by era for the same code — use the canonical mapping below, not this raw column, for reporting. | `Discharge to Home (with TCU)` | See `Disch_Type` canonical mapping below |
| `Discharge_w_in_24_hrs` | TEXT | `X` if discharged within 24 hours of admission; null otherwise. | `X` | `X`, or null |
| `LOS` | TEXT | Length of stay in days, pre-computed. Cast to NUMERIC for calculations. | `5` | Numeric, `1`–`138`+ |
| `Death_Date` | TIMESTAMP | Date of death; null if the patient survived to discharge. | `2024-10-24` | Date, or null |
| `Death_Time` | TIME | Time of death; null if the patient survived to discharge. | `23:59:59` | `HH:MM:SS`, or null |
| `Discharge_Specialty` | TEXT | NGEMR only | Discharging program/specialty — same code space as `Admission_Specialty`. | `Fast Program` | `Fast Program`, `Chronic Program`, `Healthy Aging Program` |
| `Post_Discharge_Hospital_Text` | TEXT | Destination facility name — populated only for transfer/placement discharge types (e.g. nursing home, community hospital). | `St Luke's Hospital` | Free text, or null |

#### Disch_Type — canonical mapping (for consistent reporting across SAP + NGEMR eras)

Same code set as `admission.Disch_Type`; the raw free-text column here is `Discharge_Type_Text` (`Disch_Type_1` in `admission`). Derive a single canonical `Discharge_Type` label per code rather than grouping on raw `Discharge_Type_Text` directly:

| Disch_Type | Canonical Discharge_Type | Raw `Discharge_Type_Text` values seen |
|---|---|---|
| `1` | Discharge to Home (without TCU) | `Pat discharged`, `Patient discharged`, `Discharge to Home (without TCU)` |
| `2` | Discharge to NHG Hospital | `Dis. NHG Hosp`, `Discharge to NHG Hospital` |
| `3` | Discharge to SingHealth Hospital | `Dis. Singhealth`, `Discharge to SingHealth Hospital` |
| `4` | Discharge to Private Hospital | `Dis. Pte Hosp`, `Discharge to Private Hospital` |
| `5` | Abscond | `Absconded`, `Abscond` |
| `7` | Discharge Against Medical Advice | `Dis agst advice`, `Discharge Against Medical Advice` |
| `8` | Followup at PHC | `Followup at PHC` |
| `9` | Discharge to Home (with TCU) | `Followup at SOC`, `Discharge to Home (with TCU)` |
| `10` | Followup at GP | `Followup at GP` |
| `11` | Discharge to Nursing Home | `Dis. Nursg Home`, `Discharge to Nursing Home` |
| `12` | Discharge to Hospice | `Dis. Hospices`, `Discharge to Hospice` |
| `13` | Discharge to Community Hospital | `Dis. Comm Hosp`, `Discharge to Community Hospital` |
| `14` | Discharge to Prison | `Discharge to Prison` |
| `17` | Others | `Others` |
| `18` | Social Overstay | `Social Overstay` |
| `19` | Technical Discharge | `Technical Dis.`, `Technical Discharge` |
| `20` | Home Quarantine | `Home Quarantine` |
| `21` | Nursing Home with SOC | `NursgHome w SOC`, `Nursing Home with SOC` |
| `22` | Community Hospital with SOC | `ComHosp w SOC`, `Community Hospital with SOC` |
| `23` | Discharge to Sub Acute | `Dis. SubAcute`, `Discharge to Sub Acute` |
| `24` | Discharge to AHPL Hospital | `Discharge to AHPL Hospital` |
| `27` | Discharge to NUHS Hospital | `Dis. NUHS Hosp`, `Discharge to NUHS Hospital` |
| `42` | Discharge to Transitional Care Facility | `Discharge to Transitional Care Facility` |
| `6A` | Death Non-coroner | `Death NCoroner`, `Death Non-coroner` |
| `6B` | Death Coroner | `Death Coroner` |
| `W5` | Discharge to MIC@Home | `Discharge to MIC@Home` |

SQL for deriving the canonical label:

```sql
CASE "Disch_Type"
  WHEN '1'  THEN 'Discharge to Home (without TCU)'
  WHEN '2'  THEN 'Discharge to NHG Hospital'
  WHEN '3'  THEN 'Discharge to SingHealth Hospital'
  WHEN '4'  THEN 'Discharge to Private Hospital'
  WHEN '5'  THEN 'Abscond'
  WHEN '7'  THEN 'Discharge Against Medical Advice'
  WHEN '8'  THEN 'Followup at PHC'
  WHEN '9'  THEN 'Discharge to Home (with TCU)'
  WHEN '10' THEN 'Followup at GP'
  WHEN '11' THEN 'Discharge to Nursing Home'
  WHEN '12' THEN 'Discharge to Hospice'
  WHEN '13' THEN 'Discharge to Community Hospital'
  WHEN '14' THEN 'Discharge to Prison'
  WHEN '17' THEN 'Others'
  WHEN '18' THEN 'Social Overstay'
  WHEN '19' THEN 'Technical Discharge'
  WHEN '20' THEN 'Home Quarantine'
  WHEN '21' THEN 'Nursing Home with SOC'
  WHEN '22' THEN 'Community Hospital with SOC'
  WHEN '23' THEN 'Discharge to Sub Acute'
  WHEN '24' THEN 'Discharge to AHPL Hospital'
  WHEN '27' THEN 'Discharge to NUHS Hospital'
  WHEN '42' THEN 'Discharge to Transitional Care Facility'
  WHEN '6A' THEN 'Death Non-coroner'
  WHEN '6B' THEN 'Death Coroner'
  WHEN 'W5' THEN 'Discharge to MIC@Home'
  ELSE "Discharge_Type_Text"
END AS "Discharge_Type"
```

### Diagnosis

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Pri_Diag_Code` | TEXT | Principal discharge diagnosis — ICD-10 code, code half of the pair with `Pri_Diag_Code_Text`. | `J45.9` | ICD-10 codes |
| `Pri_Diag_Code_Text` | TEXT | Principal discharge diagnosis description — description half of the pair with `Pri_Diag_Code`. | `Asthma, unspecified` | Free text |

### Referral & source

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Referral_Type` | TEXT | Resolved referral-type label, description half of the pair with `Referring_Hospital`. | `Intra-Hosp SOC` | `Intra-Hosp SOC`, `Natl Uni Health`, `Intra-Hosp A&E`, `Jurong Health`, `NHG Hosp/Inst`, `Other Govt Body`, `Intra-Hosp Ward`, `Alexandra Healt`, `NUP Polyclinics`, `Singhealth Hos`, `Others` |
| `Referring_Hospital` | TEXT | Internal hospital code, code half of the pair with `Referral_Type` and description half `Referring_Hospital_Text`. | `ZZZ2601` | `ZZZ####` codes |
| `Referring_Hospital_Text` | TEXT | Referring source free text, paired with `Referring_Hospital`. Values vary in leading whitespace and casing for the same source (e.g. `NG TENG FONG GENERAL HOSPITAL` vs `Ng Teng Fong General Hospital`) — `.strip()` + case-normalise before grouping directly. | `National University Hospital` | Free text |

### Physicians

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Attn_Physician` | TEXT | Attending physician staff ID — code half of the pair with `Attending_Physician_Name`. Staff PII. Usually but not always the same as `Discharge_Physician`. | `L16725H` (format only) | Staff ID |
| `Attending_Physician_Name` | TEXT | Attending physician name — description half of the pair with `Attn_Physician`. Staff PII — genericise in any shared examples. | `WONG XIN LIN SERENE` (format only) | Free text |
| `Discharge_Physician` | TEXT | Discharging physician staff ID — code half of the pair with `Discharge_Physician_Name`. Staff PII. | `L16725H` (format only) | Staff ID |
| `Discharge_Physician_Name` | TEXT | Discharging physician name — description half of the pair with `Discharge_Physician`. Staff PII — genericise in any shared examples. | `WONG XIN LIN SERENE` (format only) | Free text |

### Administrative / pipeline fields

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE "prelim_flag" = 'N'` when the user explicitly asks to exclude provisional records. | `N` | `N`, `Y` |
| `cnt` | INTEGER | Always `1`. Row-counter helper column — `SUM(cnt)` = row count. | `1` | `1` |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR only | 12-digit NGEMR encounter identifier. | `100220440898` | High-cardinality identifier — one per episode |

## Ward exclusions

Apply directly to `Nrs_OU` (no derived-ward step needed here, unlike `admission.Adm_Ward`):

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |
| `LCUCC` | Emergency / Urgent Care Centre |

```sql
WHERE "Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Patient class

Resolve `Disch_Class` / `Adm_Class` through `pt_class_abc` (see `references/pt-class-lookup.md`). For MOH and finance reports, apply the ICU/HD/ISO override chain on top of the resolved `Class_abc` / `Class_abc_MOH`:

```sql
CASE
  WHEN LEFT("Nrs_OU", 3) IN ('LW9','LW8') THEN 'ISO'
  WHEN LEFT("Trt_Cat", 2) = 'HD'          THEN 'HD'
  WHEN LEFT("Trt_Cat", 3) = 'CCU'         THEN 'ICU'
  ELSE "Class_abc"    -- or "Class_abc_MOH" for MOH-facing version
END AS cls_icu_iso
```

## Same-day discharge vs. discharge within 24 hours

Two distinct concepts:

```sql
-- Discharge within 24 hours of admission (uses the pre-computed flag)
WHERE "Discharge_w_in_24_hrs" = 'X'

-- Same calendar-day admission and discharge
WHERE "Adm_Date" = "Disch_Date"
```

## LOS calculations

`LOS` is pre-computed — cast to NUMERIC rather than recalculating from dates:

```sql
AVG(CAST("LOS" AS NUMERIC)) AS avg_los
PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY CAST("LOS" AS NUMERIC)) AS median_los
```

## Death flag

Prefer `Death_Date` over parsing `Discharge_Type_Text`:

```sql
CASE WHEN "Death_Date" IS NOT NULL THEN 1 ELSE 0 END AS death_flag

-- Mortality rate
SUM(CASE WHEN "Death_Date" IS NOT NULL THEN 1 ELSE 0 END)::FLOAT
  / COUNT(*) * 100 AS mortality_pct
```

## Example: average LOS by department

```sql
SELECT
  "Disch_Dept_OU_Text",
  COUNT(*) AS discharges,
  ROUND(AVG(CAST("LOS" AS NUMERIC)), 1) AS avg_los
FROM discharge
WHERE "Disch_Status" = 'A'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Disch_Date" >= '2024-01-01'
GROUP BY 1 ORDER BY avg_los DESC;
```

Add `AND "prelim_flag" = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period.

## Open items

Anything still uncertain is tracked separately in **`discharge-open-questions.md`** (same folder), not here.
