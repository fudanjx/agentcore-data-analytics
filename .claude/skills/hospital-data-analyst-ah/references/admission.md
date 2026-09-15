---
name: ah-analytics-admission
description: Column reference and SQL guidance for the ah-analytics admission table (Combined_adm — inpatient admissions). Use when writing SQL against the admission table, or when the user asks about admission volume, emergency vs elective admissions, admission source, admission ward, patient class at admission, or inpatient admission trends at Alexandra Hospital.
---

# AH Analytics — admission table (inpatient admissions)

**One row per admission episode. Primary date: `adm_date`.**

**Two source systems are combined in one file**, distinguished by admission date and by which identifier is populated:

| | Admission date | `case_no` | `pat_enc_csn_id` |
|---|---|---|---|
| **Legacy SAP era** | before 1 Jan 2023 | populated | null |
| **NGEMR/EPIC era** | from 1 Jan 2023 | null | populated |

Columns populated in only one era are marked in the **Era** column below.

## adm_ward — derived field used for ward reporting

Ward-level admission reports do **not** group by raw `adm_nrs_ou`. Production derives `adm_ward` first:

```
adm_ward = current_ward,  UNLESS adm_nrs_ou starts with "LW"
           AND adm_nrs_ou NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LCUCC')
           → then adm_ward = adm_nrs_ou
```

The final exclusion filter (`NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT', 'LCUCC')`) is applied to this **derived** `adm_ward`, not to raw `adm_nrs_ou`.

## Full column reference

`Type` is the semantic type after the casting the reporting code applies (all columns arrive as text in the raw file). "Era" marks columns only populated in one source system; blank = populated in both.

### Identifiers & demographics

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `case_no` | TEXT | SAP only | Legacy episode identifier stem, 10 digits, always starts `2800`. **Concatenate with `c` (below) for the full SAP case number** — e.g. `case_no` `2800348407` + `c` `H` → full case no. `2800348407H`. | `2800348407` | High-cardinality identifier — one per episode |
| `c` | TEXT | SAP only | Final character of the full SAP case number — concatenate onto `case_no` to form the full case no. (e.g. `2800348407` + `H` → `2800348407H`). Not a separate field: `infra/etl_ah_analytics.py`'s column-sanitisation step currently renames this column to `record_type` on load — it should not be renamed; treat it only as the `case_no` suffix. | `H` | Single letter, A–Z |
| `pat_id` | TEXT | | Internal patient ID. Format differs by era. | `Z1478946` | Letter+digit (e.g. `Z1478946`) — NGEMR; numeric (e.g. `403094`) — SAP |
| `ext_pat_id` | TEXT | | **PII — Singapore NRIC/FIN.** Treat as sensitive; don't surface raw values outside authorised use. Standard prefixes are `S`/`T` (citizens/PRs) and `F`/`G`/`M` (foreigners); other prefixes (e.g. `R`) occasionally appear for some foreign nationals. | `S1234567A` (format only) | NRIC/FIN format: 1 letter + 7 digits + 1 checksum letter |
| `resident` | TEXT | | Binary residency flag, independent of `residency` below. | `resident` | `resident`, `Non-resident` |
| `nationality` | TEXT | | nationality **code** — short half of the code/description pair with `nationality_1`. | `SG` | `SG`, `PR`, `MY`, `FR`, `FNR`, `CN`, `IN`, `BD`, `ZO` (Others), `PH`, `GB`, `TW`, `AU`, `MM`, `NO`, `TH`, `ID` |
| `nationality_1` | TEXT | | nationality **description** — full-text half of the pair with `nationality`. | `Singapore` | Free text, e.g. `Singapore`, `Malaysian`, `Chinese`, `Indian`, `Bangladeshi`, `Filipino` |
| `residency` | TEXT | NGEMR only | resident-status code (matches `Resident_MOH` in `pt-class-lookup.md`) — derived from `subvention_doc_type`. | `SG` | `SG`, `PR`, `FR`, `FNR` |
| `subvention_doc_type` | TEXT | NGEMR only | ID document type used to establish subvention eligibility — source for `residency` above. | `SG Pink IC/BC` | `SG Pink IC/BC`, `SG Blue IC`, `S Pass`, `Employment Pass`, `Other WP`, `Domestic WP`, `Long-Term Visit Pass`, `Others` |
| `age` | TEXT | | age in years at admission. Stored as text with inconsistent leading whitespace — `.strip()` before casting to INT. | `71` | Numeric, e.g. `17`–`99` |
| `sex` | TEXT | | | `M` | `M` for Male, `F` for Female |
| `postal_code` | TEXT | | **PII** — Singapore postal code (identifies to block level). Handle per data-governance rules. | `597264` (format only) | 6-digit numeric |

### Admission details

| Column | Type | Era | Description | Example | Values |
|---|---|---|---|---|---|
| `adm_date` | TIMESTAMP | | Admission date — primary date filter. Format `YYYY-MM-DD` in the raw file (SAP-era dates are `DD.MM.YYYY` before conversion — see `Date_Conversion()` in `data_prep.py`). | `2024-10-24` | Date |
| `adm_time` | TIME | | Admission time. | `08:03:00` | `HH:MM:SS` |
| `adm_dept_ou` | TEXT | | Admitting department code. Resolve via the `adm_dept_ou / dept_ou — department mapping` table below for the department name. | `LSFAMED` | See mapping table below, e.g. `LSFAMED`, `LSHAOPT`, `LSCHROGS`, `LSHAENT`, `LSCHRO`, `LSHAGERI` |
| `adm_nrs_ou` | TEXT | | Raw admitting ward/nursing-unit code. **Do not use directly for ward reporting** — see `adm_ward` derivation above. | `LW4W` | Ward codes, e.g. `LW4W`, `LW12W`, `LWASW`, `LCENDO`, `LCHAOPT` |
| `current_ward` | TEXT | | Patient's current/latest ward code — fallback in the `adm_ward` derivation. Same code space as `adm_nrs_ou`. | `LW4W` | Same code space as `adm_nrs_ou` |
| `adm_bed` | TEXT | | bed code within ward. `NONE` or null both indicate no bed assigned. | `L004004` | bed codes, or `NONE` / null |
| `adm_type` | TEXT | | Admission route/type code — see the `adm_type` codes table below. | `EM` | See `adm_type` codes table below |
| `adm_src_1` | TEXT | | Code half of a code/description pair with `adm_reason`: `1`=A&E, `2`=SOC, `3`=Ward. | `1` | `1`, `2`, `3` |
| `adm_cls` | TEXT | | Raw patient class code at admission — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`). | `SUB` | Codes defined in `pt_class_abc`: `A`, `AP`, `ARF`, `B1`, `B1P`, `B1RF`, `B2`, `B2P`, `B2RF`, `C`, `CP`, `CRF`, `NR`, `PTE`, `PTEP`, `PTRF`, `SUB`, `SUBP` |
| `wish_cls` | TEXT | | Patient's requested class — same code space as `adm_cls`, typically the coarser tiers. | `C` | `C`, `B2`, `B1`, `A` |
| `adm_trt_cat` | TEXT | | Treatment/acuity category code — same code set as `inflight.Trt_Cat`; resolve via `inflight.md`'s Trt_Cat → Acuity table (maps to L1/L2/L3/EDTU bands). | `CL3` | See `inflight.md`'s Trt_Cat → Acuity table |
| `adm_acmd_cat` | TEXT | | Accommodation category at admission. **NGEMR-era values are unreliable** -- populated with the mapped patient-class label (e.g. `B2 - SUB`), not the true bed accommodation type. See the correction below before using this column for NGEMR-era rows. | `SUB` | `ICU`, `HD`, `ISO`, `A1`, `B1`, `B2`, `C`, `SUB`, `PTE`, `OTHER` (SAP era); mapped class label for NGEMR era -- see correction below |
| `adm_status` | TEXT | | `A` = finalised, `P` = preliminary. Filter `adm_status <> 'P'` for finalised records (per `data-ontology.yaml`). | `A` | `A`, `P` |
| `adm_reason` | TEXT | NGEMR only | Description half of the code/description pair with `adm_src_1` (see above). | `SOC` | `SOC`, `A&E`, `Ward` |
| `adm_phy` / `adm_phy_name` | TEXT | NGEMR only | Admitting physician staff ID / name. Staff PII — genericise in any shared examples. | `M14796F` / `NG, JING YU` (format only) | Staff ID / name |

### Discharge details (captured on the admission record, for the same episode)

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `disch_date` | TIMESTAMP | Discharge date. Null when `adm_status = 'P'` (preliminary — not yet finalised) or when the patient remains admitted (not yet discharged) as of data extraction. | `2024-10-24` | Date, or null |
| `disch_time` | TIME | | `11:11:00` | `HH:MM:SS` |
| `disch_cls` | TEXT | Patient class at discharge — same code space and lookup as `adm_cls`. | `SUB` | Same code space as `adm_cls` |
| `disch_dept_ou` | TEXT | Discharging department code — same code space as `adm_dept_ou`. | `LSFAMED` | Same code space as `adm_dept_ou` |
| `disch_acmd_cat` | TEXT | Accommodation category at discharge — same code space as `adm_acmd_cat`. **Shares the same NGEMR-era defect** (mapped class label, not true accommodation type) -- see the correction below. | `SUB` | Same code space as `adm_acmd_cat` |
| `disch_nrs_ou` | TEXT | Discharging ward code. | `LCENDO` | Ward codes |
| `disch_bed` | TEXT | bed code at discharge. `NONE` or null both indicate no bed assigned. | `L011017` | bed codes, or `NONE` / null |
| `disch_type` | TEXT | MOH discharge-type code — see canonical mapping below for consistent reporting across SAP/NGEMR eras. | `09` | See `disch_type` canonical mapping below |
| `disch_type_1` | TEXT | Free-text discharge disposition paired with `disch_type`. Raw text varies by era for the same code (SAP abbreviated forms vs NGEMR full text) — use the canonical mapping below, not this raw column, for reporting. | `Discharge to Home (with TCU)` | See `disch_type` canonical mapping below |
| `disch_phy` / `disch_phy_name` | TEXT | Discharging physician staff ID / name. Staff PII. | `M14796F` / `NG, JING YU` (format only) | Staff ID / name |
| `disch_status` | TEXT | `A` = finalised, `P` = preliminary — same semantics as `adm_status`. | `A` | `A`, `P` |
| `infect_dis` | TEXT | Infectious-disease flag. | `IF` | `IF`, `IP` |

#### disch_type — canonical mapping (for consistent reporting across SAP + NGEMR eras)

Raw `disch_type_1` text differs by era for the same `disch_type` code. Derive a single canonical `Discharge_Type` label per code rather than grouping on raw `disch_type_1` directly:

| disch_type | Canonical Discharge_Type | Raw `disch_type_1` values seen |
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
| `diagnosis_code` | TEXT | | ICD-10 diagnosis code. | `L91.00` | ICD-10 codes |
| `diagnosis_desc` | TEXT | | Free-text description paired with `diagnosis_code`. | `Keloid` | Free text |
| `prin_diagnosis_code` | TEXT | NGEMR only | Principal diagnosis — despite the column name, holds the free-text description, not a code. Swapped with `prin_diagnosis_desc` at the source extract. | `Keloid` | Free text |
| `prin_diagnosis_desc` | TEXT | NGEMR only | Principal diagnosis — despite the column name, holds the ICD-10 code, not a description. Swapped with `prin_diagnosis_code` at the source extract. | `R07.4` | ICD-10 codes |
| `drg_code` | TEXT | SAP only | DRG code. | `K09A` | DRG codes |
| `drg_desc` | TEXT | SAP only | DRG description. | `Other Endocrine, Nutritional and Metabolic...` | Free text |

### Referral & source

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `ref_hosp_1` | TEXT | Referring source, free text — values vary in leading whitespace and casing for the same source (e.g. `NG TENG FONG GENERAL HOSPITAL` vs `Ng Teng Fong General Hospital`). `.strip()` + case-normalise before grouping directly on this column; already handled downstream via `fin_ref_hosp_inpt()` in `data_prep.py`. | `National University Hospital` | Free text, e.g. `National University Hospital`, `Intra-Dept referral SOC (Sub)`, `Intra-Dept referral A&E`, `NG TENG FONG GENERAL HOSPITAL` |
| `referral_type` | TEXT | Resolved referral-type label, description half of the pair with `referral_hospital`. | `Intra-Hosp SOC` | `Intra-Hosp SOC`, `Natl Uni Health`, `Intra-Hosp A&E`, `Jurong Health`, `NHG Hosp/Inst`, `Other Govt Body`, `Intra-Hosp Ward`, `Alexandra Healt`, `NUP Polyclinics`, `Step-Down Care` |
| `referral_hospital` | TEXT | Internal hospital code, code half of the pair with `referral_type` (1:1). | `ZZZ0802` | `ZZZ0802` (Intra-Dept SOC), `ZZZ2601` (NUH), `ZZZ0701` (Intra-Dept A&E), `ZZZ2504` (Jurong Health/NTFGH), plus other `ZZZ####` codes |

### Administrative / pipeline fields

| Column | Type | Description | Example | Values |
|---|---|---|---|---|
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE prelim_flag = 'N'` when the user explicitly asks to exclude provisional records. | `N` | `N`, `Y` |
| `cnt` | INTEGER | Always `1`. Row-counter helper column — `SUM(cnt)` = row count; used throughout the reporting pivots. | `1` | `1` |
| `pat_enc_csn_id` | TEXT | NGEMR only | 12-digit NGEMR encounter identifier. | `100220440898` | High-cardinality identifier — one per episode |

## adm_acmd_cat / disch_acmd_cat — NGEMR-era correction (bed_accom lookup)

For NGEMR-era episodes (`adm_date >= 2023-01-01`), both `adm_acmd_cat` and
`disch_acmd_cat` are populated with the mapped patient-class label (e.g. `B2 - SUB`), not
the true bed accommodation category -- don't use either directly for anything requiring
the real accommodation type.

Derive the correct value from `inflight`'s own `accom_category`, using a **last-recorded
(as-of), not exact-date** lookup: a bed's accommodation category is a near-fixed physical
property, so if `inflight` didn't record that exact bed on that exact date, the most
recent earlier reading for that same bed is a reliable stand-in.

```sql
WITH bed_accom AS (
  SELECT DISTINCT bed, inflight_date, accom_category
  FROM inflight
)
SELECT
  a.*,
  adm_ba.accom_category   AS adm_accom_category_corrected,
  disch_ba.accom_category AS disch_accom_category_corrected
FROM admission a
LEFT JOIN LATERAL (
  SELECT ba.accom_category
  FROM bed_accom ba
  WHERE ba.bed = a.adm_bed AND ba.inflight_date <= a.adm_date
  ORDER BY ba.inflight_date DESC
  LIMIT 1
) adm_ba ON true
LEFT JOIN LATERAL (
  SELECT ba.accom_category
  FROM bed_accom ba
  WHERE ba.bed = a.disch_bed AND ba.inflight_date <= a.disch_date
  ORDER BY ba.inflight_date DESC
  LIMIT 1
) disch_ba ON true
```

SAP-era `adm_acmd_cat`/`disch_acmd_cat` are assumed reliable as-is and don't need this
correction. Use `COALESCE(adm_ba.accom_category, a.adm_acmd_cat)` (and the discharge
equivalent) when a fallback is needed -- this only fires for a bed with **no `inflight`
reading at all before the target date** (e.g. a bed newly commissioned that day), a much
narrower gap than the exact-date-match approach: it also resolves `inflight.md`'s same-day
top-up union, since a same-day admit+discharge case's bed will normally still have earlier
`inflight` history to carry forward, even though it has no `inflight` row on that exact
date. See `inflight.md`'s top-up section for how this applies there.

## adm_dept_ou / dept_ou — department mapping (Subspec)

Same code space as `discharge.adm_dept_ou`/`dept_ou`.

| dept_ou | Dept_Name |
|---|---|
| `LSFAGS` | Fast General Surgery |
| `LSFAMED` | Fast Medicine |
| `LSCHROGS` | Chronic General Surgery |
| `LSCHRO` | Chronic |
| `LSPALL` | Palliative Care |
| `LSWELL` | Wellness |
| `LSWEGYNA` | Wellness Gynaecology |
| `LSANAE` | Anaesthesia |
| `LSUCC` | Urgent Care |
| `LSHAOPT` | HA Opthalmology |
| `LSHAENT` | HA Otolaryngology |
| `LSHAOMS` | HA Oral Maxil Surg |
| `LSHAPERI` | HA Periodontics |
| `LSHAPROS` | HA Prosthodontics |
| `LSHAENDO` | HA Endodontics |
| `LSHAGDEN` | HA General Dentistry |
| `LSHAGDGD` | HA Geriatric Dentistry_PG |
| `LSHADEN` | HA Dental Services |
| `LSHAGERI` | HA Geriatric Medicine |
| `LSHAPSYM` | HA Psychological Meds |
| `LSHAORTH` | HA General Orthopaedic |
| `LSHAAREC` | HA Adult Reconstruction |
| `LSEDTU` | Extended Diag Treatment |
| `LSFARHM` | Fast Rehabilitation Med |
| `LSHARHM` | HA Rehabilitation Med |
| `LSHAURO` | HA Urology |
| `LSFAVAS` | Fast Vascular Surgery |
| `LSCHCACA` | Chronic Cardiology |
| `LSCHPLS` | Plastic Surgery |
| `LSHAHRM` | Hand Surgery |
| `LSFATHO` | Fast Thoracic Surgery |
| `LSFANS` | Fast Neurosurgery |
| `LSAMBS` | Ambulatory Services |

## Ward exclusions

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |
| `LCUCC` | Emergency / Urgent Care Centre |

## adm_type codes

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

The "inpatient-only" filter, `adm_type IN ('EM|SD|DI|EL|TA|RA')` to be included; Excludes `DO` (Day Surgery OP), `DS` (Day Surgery), `ES` (Endoscopy) and `SO` (Social Overstay) — these are day-case/procedural/overstay types, not true inpatient admissions.

## Patient class

Resolve `adm_cls` through `pt_class_abc` (see `references/pt-class-lookup.md`). Quick paying-status split:

```sql
CASE
  WHEN adm_cls IN ('B2','B2P','C') THEN 'Subsidised'
  ELSE 'Paying'
END AS paying_status
```

## Example: monthly admissions by ward (replicates `adm_by_ward`)

```sql
WITH adm_ward AS (
  SELECT *,
    CASE
      WHEN LEFT(adm_nrs_ou, 2) = 'LW'
           AND adm_nrs_ou NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
        THEN adm_nrs_ou
      ELSE current_ward
    END AS adm_ward
  FROM admission
  WHERE adm_status != 'P'
    AND adm_type IN ('EM','EL','SD','DI','TA','RA')
)
SELECT
  DATE_TRUNC('month', adm_date) AS month,
  adm_ward,
  COUNT(*) AS admissions
FROM adm_ward
WHERE adm_ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT', 'LCUCC')
GROUP BY 1, 2 ORDER BY 1;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period.
