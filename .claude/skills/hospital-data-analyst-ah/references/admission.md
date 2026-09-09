---
name: ah-analytics-admission
description: Column reference and SQL guidance for the ah-analytics admission table (Combined_adm — inpatient admissions). Use when writing SQL against the admission table, or when the user asks about admission volume, emergency vs elective admissions, admission source, admission ward, patient class at admission, or inpatient admission trends at Alexandra Hospital.
---

# AH Analytics — admission table (inpatient admissions)

**One row per admission episode. Primary date: `Adm_Date`.**

**Two source systems are combined in one file**, distinguished by admission date and by which identifier is populated:

| | Admission date | `Case_No` | `PAT_ENC_CSN_ID` |
|---|---|---|---|
| **Legacy SAP era** | before 1 Jan 2023 | populated | null |
| **NGEMR/EPIC era** | from 1 Jan 2023 | null | populated |

Columns populated in only one era are marked in the **Era** column below.

## Adm_Ward — derived field used for ward reporting

Ward-level admission reports do **not** group by raw `Adm_Nrs_OU`. Production derives `Adm_Ward` first:

```
Adm_Ward = Current_Ward,  UNLESS Adm_Nrs_OU starts with "LW"
           AND Adm_Nrs_OU NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LCUCC')
           → then Adm_Ward = Adm_Nrs_OU
```

The final exclusion filter (`NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT', 'LCUCC')`) is applied to this **derived** `Adm_Ward`, not to raw `Adm_Nrs_OU`.

## Full column reference

`Type` is the semantic type after the casting the reporting code applies (all columns arrive as text in the raw file). "Era" marks columns only populated in one source system; blank = populated in both.

### Identifiers & demographics

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `Case_No` | TEXT | SAP only | Legacy episode identifier stem, 10 digits, always starts `2800`. **Concatenate with `C` (below) for the full SAP case number** — e.g. `Case_No` `2800348407` + `C` `H` → full case no. `2800348407H`. | `2800348407` | High-cardinality identifier — one per episode |
| `C` | TEXT | SAP only | Final character of the full SAP case number — concatenate onto `Case_No` to form the full case no. (e.g. `2800348407` + `H` → `2800348407H`). Not a separate field: `infra/etl_ah_analytics.py`'s column-sanitisation step currently renames this column to `record_type` on load — it should not be renamed; treat it only as the `Case_No` suffix. | `H` | Single letter, A–Z |
| `Pat_ID` | TEXT | | Internal patient ID. Format differs by era. | `Z1478946` | Letter+digit (e.g. `Z1478946`) — NGEMR; numeric (e.g. `403094`) — SAP |
| `Ext_Pat_ID` | TEXT | | **PII — Singapore NRIC/FIN.** Treat as sensitive; don't surface raw values outside authorised use. Standard prefixes are `S`/`T` (citizens/PRs) and `F`/`G`/`M` (foreigners); other prefixes (e.g. `R`) occasionally appear for some foreign nationals. | `S1234567A` (format only) | NRIC/FIN format: 1 letter + 7 digits + 1 checksum letter |
| `Resident` | TEXT | | Binary residency flag, independent of `Residency` below. | `Resident` | `Resident`, `Non-Resident` |
| `Nationality` | TEXT | | Nationality **code** — short half of the code/description pair with `Nationality_1`. | `SG` | `SG`, `PR`, `MY`, `FR`, `FNR`, `CN`, `IN`, `BD`, `ZO` (Others), `PH`, `GB`, `TW`, `AU`, `MM`, `NO`, `TH`, `ID` |
| `Nationality_1` | TEXT | | Nationality **description** — full-text half of the pair with `Nationality`. | `Singapore` | Free text, e.g. `Singapore`, `Malaysian`, `Chinese`, `Indian`, `Bangladeshi`, `Filipino` |
| `Residency` | TEXT | NGEMR only | Resident-status code (matches `Resident_MOH` in `pt-class-lookup.md`) — derived from `Subvention_Doc_Type`. | `SG` | `SG`, `PR`, `FR`, `FNR` |
| `Subvention_Doc_Type` | TEXT | NGEMR only | ID document type used to establish subvention eligibility — source for `Residency` above. | `SG Pink IC/BC` | `SG Pink IC/BC`, `SG Blue IC`, `S Pass`, `Employment Pass`, `Other WP`, `Domestic WP`, `Long-Term Visit Pass`, `Others` |
| `Age` | TEXT | | Age in years at admission. Stored as text with inconsistent leading whitespace — `.strip()` before casting to INT. | `71` | Numeric, e.g. `17`–`99` |
| `Sex` | TEXT | | | `M` | `M` for Male, `F` for Female |
| `Postal_Code` | TEXT | | **PII** — Singapore postal code (identifies to block level). Handle per data-governance rules. | `597264` (format only) | 6-digit numeric |

### Admission details

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `Adm_Date` | TIMESTAMP | | Admission date — primary date filter. Format `YYYY-MM-DD` in the raw file (SAP-era dates are `DD.MM.YYYY` before conversion — see `Date_Conversion()` in `data_prep.py`). | `2024-10-24` | Date |
| `Adm_Time` | TIME | | Admission time. | `08:03:00` | `HH:MM:SS` |
| `Adm_Dept_OU` | TEXT | | Admitting department code. Resolve via the `Subspec` sheet in `Class.xlsx` for the department name. | `LSFAMED` | Codes defined in the `Subspec` sheet of `Class.xlsx`, e.g. `LSFAMED`, `LSHAOPT`, `LSCHROGS`, `LSHAENT`, `LSCHRO`, `LSHAGERI` |
| `Adm_Nrs_OU` | TEXT | | Raw admitting ward/nursing-unit code. **Do not use directly for ward reporting** — see `Adm_Ward` derivation above. | `LW4W` | Ward codes, e.g. `LW4W`, `LW12W`, `LWASW`, `LCENDO`, `LCHAOPT` |
| `Current_Ward` | TEXT | | Patient's current/latest ward code — fallback in the `Adm_Ward` derivation. Same code space as `Adm_Nrs_OU`. | `LW4W` | Same code space as `Adm_Nrs_OU` |
| `Adm_Bed` | TEXT | | Bed code within ward. `NONE` or null both indicate no bed assigned. | `L004004` | Bed codes, or `NONE` / null |
| `Adm_Type` | TEXT | | Admission route/type code — see the `Adm_Type` codes table below. | `EM` | See `Adm_Type` codes table below |
| `Adm_Src_1` | TEXT | | Code half of a code/description pair with `Adm_Reason`: `1`=A&E, `2`=SOC, `3`=Ward. | `1` | `1`, `2`, `3` |
| `Adm_Cls` | TEXT | | Raw patient class code at admission — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`). | `SUB` | Codes defined in `pt_class_abc`: `A`, `AP`, `ARF`, `B1`, `B1P`, `B1RF`, `B2`, `B2P`, `B2RF`, `C`, `CP`, `CRF`, `NR`, `PTE`, `PTEP`, `PTRF`, `SUB`, `SUBP` |
| `Wish_Cls` | TEXT | | Patient's requested class — same code space as `Adm_Cls`, typically the coarser tiers. | `C` | `C`, `B2`, `B1`, `A` |
| `Adm_Trt_Cat` | TEXT | | Treatment/acuity category code — resolve via the `Acuity` sheet in `Class.xlsx` (maps to L1/L2/L3/EDTU bands). | `CL3` | Codes defined in the `Acuity` sheet of `Class.xlsx` |
| `Adm_Acmd_Cat` | TEXT | | Accommodation category at admission. | `SUB` | `ICU`, `HD`, `ISO`, `A1`, `B1`, `B2`, `C`, `SUB`, `PTE`, `OTHER` |
| `Adm_Status` | TEXT | | `A` = finalised, `P` = preliminary. Filter `Adm_Status <> 'P'` for finalised records (per `data-ontology.yaml`). | `A` | `A`, `P` |
| `Adm_Reason` | TEXT | NGEMR only | Description half of the code/description pair with `Adm_Src_1` (see above). | `SOC` | `SOC`, `A&E`, `Ward` |
| `Adm_Phy` / `Adm_Phy_Name` | TEXT | NGEMR only | Admitting physician staff ID / name. Staff PII — genericise in any shared examples. | `M14796F` / `NG, JING YU` (format only) | Staff ID / name |

### Discharge details (captured on the admission record, for the same episode)

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Disch_Date` | TIMESTAMP | Discharge date. Null when `Adm_Status = 'P'` (preliminary — not yet finalised) or when the patient remains admitted (not yet discharged) as of data extraction. | `2024-10-24` | Date, or null |
| `Disch_Time` | TIME | | `11:11:00` | `HH:MM:SS` |
| `Disch_Cls` | TEXT | Patient class at discharge — same code space and lookup as `Adm_Cls`. | `SUB` | Same code space as `Adm_Cls` |
| `Disch_Dept_OU` | TEXT | Discharging department code — same code space as `Adm_Dept_OU`. | `LSFAMED` | Same code space as `Adm_Dept_OU` |
| `Disch_Acmd_Cat` | TEXT | Accommodation category at discharge — same code space as `Adm_Acmd_Cat`. | `SUB` | Same code space as `Adm_Acmd_Cat` |
| `Disch_Nrs_OU` | TEXT | Discharging ward code. | `LCENDO` | Ward codes |
| `Disch_Bed` | TEXT | Bed code at discharge. `NONE` or null both indicate no bed assigned. | `L011017` | Bed codes, or `NONE` / null |
| `Disch_Type` | TEXT | MOH discharge-type code — see canonical mapping below for consistent reporting across SAP/NGEMR eras. | `09` | See `Disch_Type` canonical mapping below |
| `Disch_Type_1` | TEXT | Free-text discharge disposition paired with `Disch_Type`. Raw text varies by era for the same code (SAP abbreviated forms vs NGEMR full text) — use the canonical mapping below, not this raw column, for reporting. | `Discharge to Home (with TCU)` | See `Disch_Type` canonical mapping below |
| `Disch_Phy` / `Disch_Phy_Name` | TEXT | Discharging physician staff ID / name. Staff PII. | `M14796F` / `NG, JING YU` (format only) | Staff ID / name |
| `Disch_Status` | TEXT | `A` = finalised, `P` = preliminary — same semantics as `Adm_Status`. | `A` | `A`, `P` |
| `Infect_Dis` | TEXT | Infectious-disease flag. | `IF` | `IF`, `IP` |

#### Disch_Type — canonical mapping (for consistent reporting across SAP + NGEMR eras)

Raw `Disch_Type_1` text differs by era for the same `Disch_Type` code. Derive a single canonical `Discharge_Type` label per code rather than grouping on raw `Disch_Type_1` directly:

| Disch_Type | Canonical Discharge_Type | Raw `Disch_Type_1` values seen |
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

### Diagnosis & clinical coding

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `Diagnosis_Code` | TEXT | | ICD-10 diagnosis code. | `L91.00` | ICD-10 codes |
| `Diagnosis_Desc` | TEXT | | Free-text description paired with `Diagnosis_Code`. | `Keloid` | Free text |
| `Prin_Diagnosis_Code` | TEXT | NGEMR only | Principal diagnosis — despite the column name, holds the free-text description, not a code. Swapped with `Prin_Diagnosis_Desc` at the source extract. | `Keloid` | Free text |
| `Prin_Diagnosis_Desc` | TEXT | NGEMR only | Principal diagnosis — despite the column name, holds the ICD-10 code, not a description. Swapped with `Prin_Diagnosis_Code` at the source extract. | `R07.4` | ICD-10 codes |
| `DRG_Code` | TEXT | SAP only | DRG code. | `K09A` | DRG codes |
| `DRG_Desc` | TEXT | SAP only | DRG description. | `Other Endocrine, Nutritional and Metabolic...` | Free text |

### Referral & source

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `Ref_Hosp_1` | TEXT | Referring source, free text — values vary in leading whitespace and casing for the same source (e.g. `NG TENG FONG GENERAL HOSPITAL` vs `Ng Teng Fong General Hospital`). `.strip()` + case-normalise before grouping directly on this column; already handled downstream via `fin_ref_hosp_inpt()` in `data_prep.py`. | `National University Hospital` | Free text, e.g. `National University Hospital`, `Intra-Dept referral SOC (Sub)`, `Intra-Dept referral A&E`, `NG TENG FONG GENERAL HOSPITAL` |
| `Referral_type` | TEXT | Resolved referral-type label, description half of the pair with `Referral_Hospital`. | `Intra-Hosp SOC` | `Intra-Hosp SOC`, `Natl Uni Health`, `Intra-Hosp A&E`, `Jurong Health`, `NHG Hosp/Inst`, `Other Govt Body`, `Intra-Hosp Ward`, `Alexandra Healt`, `NUP Polyclinics`, `Step-Down Care` |
| `Referral_Hospital` | TEXT | Internal hospital code, code half of the pair with `Referral_type` (1:1). | `ZZZ0802` | `ZZZ0802` (Intra-Dept SOC), `ZZZ2601` (NUH), `ZZZ0701` (Intra-Dept A&E), `ZZZ2504` (Jurong Health/NTFGH), plus other `ZZZ####` codes |

### Administrative / pipeline fields

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE "prelim_flag" = 'N'` when the user explicitly asks to exclude provisional records. | `N` | `N`, `Y` |
| `cnt` | INTEGER | Always `1`. Row-counter helper column — `SUM(cnt)` = row count; used throughout the reporting pivots. | `1` | `1` |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR only | 12-digit NGEMR encounter identifier. | `100220440898` | High-cardinality identifier — one per episode |

## Ward exclusions

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |
| `LCUCC` | Emergency / Urgent Care Centre |

## Adm_Type codes

| Code | Meaning |
|------|---------|
| `DI` | DS turn Inpat. |
| `DO` | Day Surgery OP |
| `DS` | Day Surgery |
| `EL` | Elective inpatient |
| `EM` | Emergency |
| `ES` | Endoscopy |
| `RA` | Repeat Adm. |
| `SD` | Same Day Adm. |
| `SO` | Social Overstay |
| `TA` | Technical Adm. |

The "inpatient-only" filter, `Adm_Type IN ('EM|SD|DI|EL|TA|RA')` to be included; Excludes `DO` (Day Surgery OP), `DS` (Day Surgery), `ES` (Endoscopy) and `SO` (Social Overstay) — these are day-case/procedural/overstay types, not true inpatient admissions.

## Patient class

Resolve `Adm_Cls` through `pt_class_abc` (see `references/pt-class-lookup.md`). Quick paying-status split:

```sql
CASE
  WHEN "Adm_Cls" IN ('B2','B2P','C') THEN 'Subsidised'
  ELSE 'Paying'
END AS paying_status
```

## Example: monthly admissions by ward (replicates `adm_by_ward`)

```sql
WITH adm_ward AS (
  SELECT *,
    CASE
      WHEN LEFT("Adm_Nrs_OU", 2) = 'LW'
           AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
        THEN "Adm_Nrs_OU"
      ELSE "Current_Ward"
    END AS "Adm_Ward"
  FROM admission
  WHERE "Adm_Status" != 'P'
    AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
)
SELECT
  DATE_TRUNC('month', "Adm_Date") AS month,
  "Adm_Ward",
  COUNT(*) AS admissions
FROM adm_ward
WHERE "Adm_Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT', 'LCUCC')
GROUP BY 1, 2 ORDER BY 1;
```

Add `AND "prelim_flag" = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period.
