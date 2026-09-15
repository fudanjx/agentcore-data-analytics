---
name: ah-analytics-discharge
description: Column reference and SQL guidance for the ah-analytics discharge table (Combined_dc — inpatient discharges). Use when writing SQL against the discharge table, or when the user asks about length of stay, discharge disposition, in-hospital mortality, discharge destination, or patient outcomes after inpatient admission at Alexandra Hospital.
---

# AH Analytics — discharge table (inpatient discharges)

**One row per discharged episode. Primary date: `disch_date`.**
Use this table (not `admission`) for outcome questions: los, death, discharge destination.

**Two source systems are combined in one file**, distinguished by admission date and by which identifier is populated:

| | Admission date | `case_no` | `pat_enc_csn_id` |
|---|---|---|---|
| **Legacy SAP era** | before 1 Jan 2023 | populated | null |
| **NGEMR/EPIC era** | from 1 Jan 2023 | null | populated |

Columns populated in only one era are marked in the **Era** column below.

## Full column reference

`Type` is the semantic type after the casting the reporting code applies (all columns arrive as text in the raw file). "Era" marks columns only populated in one source system; blank = populated in both.

Note: several columns hold the same information as their `admission` table counterpart but under a **different name** — `patient_id` (not `pat_id`), `ext_pat_no` (not `ext_pat_id`), `postal` (not `postal_code`). Don't assume shared column names across the two tables.

### Identifiers & demographics

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `case_no` | TEXT | SAP only | Legacy episode identifier stem. **Concatenate with `c` (below) for the full SAP case number**, same as in `admission`. | `2800348407` | High-cardinality identifier — one per episode |
| `c` | TEXT | SAP only | Final character of the full SAP case number — concatenate onto `case_no`, don't treat as a separate field. | `H` | Single letter, A–Z |
| `patient_id` | TEXT | | Internal patient ID (called `pat_id` in `admission`). | `Z1478946` | Letter+digit or numeric identifier |
| `ext_pat_no` | TEXT | | **PII — Singapore NRIC/FIN** (called `ext_pat_id` in `admission`). Treat as sensitive; don't surface raw values outside authorised use. | `S1234567A` (format only) | NRIC/FIN format: 1 letter + 7 digits + 1 checksum letter |
| `nationality` | TEXT | SAP only | nationality code. | `SG` | `SG`, `PR`, `FR`, `FNR` |
| `age` | TEXT | | age at discharge. | `71` | Numeric |
| `sex` | TEXT | | | `M` | `M` for Male, `F` for Female |
| `postal` | TEXT | | **PII** — Singapore postal code (called `postal_code` in `admission`). Handle per data-governance rules. | `597264` (format only) | 6-digit numeric |

### Admission details (captured on the discharge record, for the same episode)

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `adm_date` | TIMESTAMP | Admission date. | `2024-10-24` | Date |
| `adm_time` | TIME | Admission time. | `08:03:00` | `HH:MM:SS` |
| `adm_dept_ou` | TEXT | Admitting department code — code half of the pair with `adm_dept_ou_text`. Same code space as `admission.adm_dept_ou` — resolve via `admission.md`'s Subspec mapping table. | `LSFAMED` | See `admission.md`'s Subspec mapping table |
| `adm_dept_ou_text` | TEXT | Admitting department name — description half of the pair with `adm_dept_ou`. Can differ from `dept_ou`/`disch_dept_ou_text` (below) when the patient's department changed during the stay. | `Fast Medicine` | Free text |
| `adm_nurs_ou` | TEXT | Admitting ward code — code half of the pair with `adm_nurs_ou_text`. Can differ from `nrs_ou` (below) when the patient's ward changed during the stay. | `LW13W` | Ward codes |
| `adm_nurs_ou_text` | TEXT | Admitting ward name — description half of the pair with `adm_nurs_ou`. | `Alex Ward 13` | Free text |
| `adm_bed` | TEXT | Bed code at admission. `NONE` or null both indicate no bed assigned. | `L004004` | Bed codes, or `NONE` / null |
| `adm_type` | TEXT | Admission route/type code — same code set as `admission.adm_type`. | `EM` | `DI`, `DO`, `DS`, `EL`, `EM`, `ES`, `RA`, `SD`, `SO`, `TA` |
| `adm_class` | TEXT | Raw patient class at admission — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`). Can differ from `disch_class` (below) when the patient's class changed during the stay. | `SUB` | Codes defined in `pt_class_abc` |
| `adm_trt_cat` | TEXT | Treatment/acuity category at admission — same code set as `inflight.trt_cat`; resolve via `inflight.md`'s trt_cat → Acuity table. Can differ from `trt_cat` (below) when acuity changed during the stay. | `CL3` | See `inflight.md`'s trt_cat → Acuity table |
| `adm_status` | TEXT | Admission status. Only `A` (`Actual`) observed for this table. | `A` | `A` |
| `adm_status_text` | TEXT | Description half of the pair with `adm_status`. | `Actual` | `Actual` |
| `adm_physician` | TEXT | Admitting physician staff ID — code half of the pair with `admitting_physician_name`. Staff PII. | `M18760G` (format only) | Staff ID |
| `admitting_physician_name` | TEXT | Admitting physician name — description half of the pair with `adm_physician`. Staff PII — genericise in any shared examples. | `TING, YANGHAN, YOHANES` (format only) | Free text |
| `admission_specialty` | TEXT | NGEMR only | Admitting program/specialty. | `Fast Program` | `Fast Program`, `Chronic Program`, `Healthy Aging Program` |

### Discharge details

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `disch_date` | TIMESTAMP | Discharge date — primary date filter. | `2024-10-24` | Date |
| `disch_time` | TIME | Discharge time. | `11:11:00` | `HH:MM:SS` |
| `dept_ou` | TEXT | Discharging department code — code half of the pair with `disch_dept_ou_text`. | `LSFAMED` | Same code space as `adm_dept_ou` |
| `disch_dept_ou_text` | TEXT | Discharging department name — description half of the pair with `dept_ou`. | `Fast Medicine` | Free text |
| `nrs_ou` | TEXT | Discharging ward code — code half of the pair with `disch_nrs_ou_text`. Apply the ward exclusion filter here (see below). | `LW4W` | Same code space as `adm_nurs_ou` |
| `disch_nrs_ou_text` | TEXT | Discharging ward name — description half of the pair with `nrs_ou`. Casing/spacing varies by era for the same ward (e.g. `ALEX WARD 12` vs `Alex Ward 12`) — normalise before grouping directly. | `Alex Ward 4` | Free text |
| `disch_bed` | TEXT | Bed code at discharge. `NONE` or null both indicate no bed assigned. | `L011017` | Bed codes, or `NONE` / null |
| `disch_class` | TEXT | Raw patient class at discharge — resolve through `pt_class_abc`. | `SUB` | Codes defined in `pt_class_abc` |
| `trt_cat` | TEXT | Treatment/acuity category at discharge — same code set as `inflight.trt_cat`; resolve via `inflight.md`'s trt_cat → Acuity table. Identical to `discharge_acuity_level` (below) — use one, not both. | `CL3` | See `inflight.md`'s trt_cat → Acuity table |
| `discharge_acuity_level` | TEXT | Alias of `trt_cat` — always the same value. Kept for backward compatibility only. | `CL3` | Same values as `trt_cat` |
| `disch_status` | TEXT | Discharge status. Filter `disch_status = 'A'` for finalised discharge records — part of the standard filter set alongside `adm_type` and `nrs_ou` below. | `A` | `A` |
| `disch_type` | TEXT | MOH discharge-type code — see canonical mapping below for consistent reporting across SAP/NGEMR eras. Same code set as `admission.disch_type`. | `09` | See `disch_type` canonical mapping below |
| `discharge_type_text` | TEXT | Free-text discharge disposition paired with `disch_type` (called `Disch_Type_1` in `admission`). Raw text varies by era for the same code — use the canonical mapping below, not this raw column, for reporting. | `Discharge to Home (with TCU)` | See `disch_type` canonical mapping below |
| `discharge_w_in_24_hrs` | TEXT | `X` if discharged within 24 hours of admission; null otherwise. | `X` | `X`, or null |
| `los` | TEXT | Length of stay in days, pre-computed. Cast to NUMERIC for calculations. | `5` | Numeric, `1`–`138`+ |
| `death_date` | TIMESTAMP | Date of death; null if the patient survived to discharge. | `2024-10-24` | Date, or null |
| `death_time` | TIME | Time of death; null if the patient survived to discharge. | `23:59:59` | `HH:MM:SS`, or null |
| `discharge_specialty` | TEXT | NGEMR only | Discharging program/specialty — same code space as `admission_specialty`. | `Fast Program` | `Fast Program`, `Chronic Program`, `Healthy Aging Program` |
| `post_discharge_hospital_text` | TEXT | Destination facility name — populated only for transfer/placement discharge types (e.g. nursing home, community hospital). | `St Luke's Hospital` | Free text, or null |

#### disch_type — canonical mapping (for consistent reporting across SAP + NGEMR eras)

Same code set as `admission.disch_type`; the raw free-text column here is `discharge_type_text` (`disch_type_1` in `admission`). Derive a single canonical `Discharge_Type` label per code rather than grouping on raw `discharge_type_text` directly:

| disch_type | Canonical Discharge_Type | Raw `discharge_type_text` values seen |
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
CASE disch_type
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
  ELSE discharge_type_text
END AS discharge_type
```

### Diagnosis

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `pri_diag_code` | TEXT | Principal discharge diagnosis — ICD-10 code, code half of the pair with `pri_diag_code_text`. | `J45.9` | ICD-10 codes |
| `pri_diag_code_text` | TEXT | Principal discharge diagnosis description — description half of the pair with `pri_diag_code`. | `Asthma, unspecified` | Free text |

### Referral & source

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `referral_type` | TEXT | Resolved referral-type label, description half of the pair with `referring_hospital`. | `Intra-Hosp SOC` | `Intra-Hosp SOC`, `Natl Uni Health`, `Intra-Hosp A&E`, `Jurong Health`, `NHG Hosp/Inst`, `Other Govt Body`, `Intra-Hosp Ward`, `Alexandra Healt`, `NUP Polyclinics`, `Singhealth Hos`, `Others` |
| `referring_hospital` | TEXT | Internal hospital code, code half of the pair with `referral_type` and description half `referring_hospital_text`. | `ZZZ2601` | `ZZZ####` codes |
| `referring_hospital_text` | TEXT | Referring source free text, paired with `referring_hospital`. Values vary in leading whitespace and casing for the same source (e.g. `NG TENG FONG GENERAL HOSPITAL` vs `Ng Teng Fong General Hospital`) — `.strip()` + case-normalise before grouping directly. | `National University Hospital` | Free text |

### Physicians

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `attn_physician` | TEXT | Attending physician staff ID — code half of the pair with `attending_physician_name`. Staff PII. Usually but not always the same as `discharge_physician`. | `L16725H` (format only) | Staff ID |
| `attending_physician_name` | TEXT | Attending physician name — description half of the pair with `attn_physician`. Staff PII — genericise in any shared examples. | `WONG XIN LIN SERENE` (format only) | Free text |
| `discharge_physician` | TEXT | Discharging physician staff ID — code half of the pair with `discharge_physician_name`. Staff PII. | `L16725H` (format only) | Staff ID |
| `discharge_physician_name` | TEXT | Discharging physician name — description half of the pair with `discharge_physician`. Staff PII — genericise in any shared examples. | `WONG XIN LIN SERENE` (format only) | Free text |

### Administrative / pipeline fields

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE prelim_flag = 'N'` when the user explicitly asks to exclude provisional records. | `N` | `N`, `Y` |
| `cnt` | INTEGER | Always `1`. Row-counter helper column — `SUM(cnt)` = row count. | `1` | `1` |
| `pat_enc_csn_id` | TEXT | NGEMR only | 12-digit NGEMR encounter identifier. | `100220440898` | High-cardinality identifier — one per episode |

## Ward exclusions

Apply directly to `nrs_ou` (no derived-ward step needed here, unlike `admission.Adm_Ward`):

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |
| `LCUCC` | Emergency / Urgent Care Centre |

```sql
WHERE nrs_ou NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Patient class

Resolve `disch_class` / `adm_class` through `pt_class_abc` (see `references/pt-class-lookup.md`). For MOH and finance reports, apply the ICU/HD/ISO override chain on top of the resolved `class_abc` / `class_abc_moh`:

```sql
CASE
  WHEN LEFT(nrs_ou, 3) IN ('LW9','LW8') THEN 'ISO'
  WHEN LEFT(trt_cat, 2) = 'HD'          THEN 'HD'
  WHEN LEFT(trt_cat, 3) = 'CCU'         THEN 'ICU'
  ELSE class_abc    -- or class_abc_moh for MOH-facing version
END AS cls_icu_iso
```

## Same-day discharge vs. discharge within 24 hours

Two distinct concepts:

```sql
-- Discharge within 24 hours of admission (uses the pre-computed flag)
WHERE discharge_w_in_24_hrs = 'X'

-- Same calendar-day admission and discharge
WHERE adm_date = disch_date
```

## los calculations

`los` is pre-computed — cast to NUMERIC rather than recalculating from dates:

```sql
AVG(CAST(los AS NUMERIC)) AS avg_los
PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY CAST(los AS NUMERIC)) AS median_los
```

## Death flag

Prefer `death_date` over parsing `discharge_type_text`:

```sql
CASE WHEN death_date IS NOT NULL THEN 1 ELSE 0 END AS death_flag

-- Mortality rate
SUM(CASE WHEN death_date IS NOT NULL THEN 1 ELSE 0 END)::FLOAT
  / COUNT(*) * 100 AS mortality_pct
```

## Example: average los by department

```sql
SELECT
  disch_dept_ou_text,
  COUNT(*) AS discharges,
  ROUND(AVG(CAST(los AS NUMERIC)), 1) AS avg_los
FROM discharge
WHERE disch_status = 'A'
  AND adm_type IN ('EM','EL','SD','DI','TA','RA')
  AND nrs_ou NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND disch_date >= '2024-01-01'
GROUP BY 1 ORDER BY avg_los DESC;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period.
