# AH Analytics Data Dictionary

Column interpretation guide, processing logic, and inclusion/exclusion criteria for each source parquet file.
Derived from analysis of the HIM reporting scripts in this directory.

---

## 1. Combined_SOC — Specialist Outpatient Clinic Visits

**Database table:** `outpatient`  
**Primary date column:** `Visit_Date`  
**Scripts:** `NGEMR_Procedure_SOC_UCC_Consolidated.py`, `NGEMR_Columns_Map_v1.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Appointment status | `APPT_STATUS` not in `['Booked', 'Cancelled']` OR `Status == 'A'` | Completed/actual visits only; pending appointments excluded |
| Visit type | `Visit_Type` in `['FV','RV','FW','RW','DF','DR','FD','RD']` | Clinical visits only; administrative types excluded |
| Treatment category | `Trt_Cat != 'NC'` | Exclude non-consult (administrative slots) |
| Date window | `Visit_Date` within 12-month rolling window | Standard reporting period |

### Exclusion Criteria
- `Status == 'P'` — pending (not yet completed)
- `Trt_Cat == 'NC'` — non-consult slot
- `APPT_STATUS` in `['Booked', 'Cancelled']`

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Patient encounter number (prefix `2800` or `8280` for AH) |
| `Visit_No` | TEXT | Sequential visit counter for the patient |
| `Visit_Date` | TIMESTAMP | Date the clinic visit occurred |
| `Visit_Time` | TIME | Actual visit time |
| `APPT_TIME` | TIME | Scheduled appointment time |
| `Appt_Creation_Date` | TIMESTAMP | When the appointment was booked |
| `APPT_REQUEST_DTTM` | TIMESTAMP | When appointment request was initiated |
| `Visit_Type` | TEXT | Visit classification — see mapping below |
| `APPT_STATUS` | TEXT | Appointment lifecycle status (`Completed`, `Cancelled`, `Booked`, etc.) |
| `Trt_Cat` | TEXT | Treatment category code (e.g. `ASCRV`=repeat visit, `NC`=non-consult) |
| `Class` | TEXT | Patient class at visit (`SUB`=subsidised, `A`, `B1`, `B2`, `C`) |
| `Trt_OU` | TEXT | Treating organisational unit (clinic name) |
| `Trt_OU_ID` | TEXT | Treating OU code |
| `Clinical_Dept` | TEXT | Clinical department name |
| `Clinical_Dept_ID` | TEXT | Clinical department code |
| `Sub-Specialty` | TEXT | Sub-specialty within department |
| `Sub-Specialty_ID` | TEXT | Sub-specialty code |
| `Attn_MCR` | TEXT | Attending physician MCR registration number |
| `Attn_Phy` | TEXT | Attending physician name |
| `Referral_type` | TEXT | Referral source type (Intra-Dept, GP, Polyclinic, etc.) |
| `Referral_Hospital` | TEXT | Referring institution name/code |
| `Pri_Diag_Code` | TEXT | Primary diagnosis ICD-10 code |
| `Pri_Diag_Desc` | TEXT | Primary diagnosis description |
| `Age` | TEXT | Patient age at visit |
| `Sex` | TEXT | Patient sex (`M`/`F`) |
| `Race` | TEXT | Patient race |
| `Nationality` | TEXT | Patient nationality |
| `Postal_Code` | TEXT | Patient postal code (masked to 6-digit prefix) |
| `cnt` | INTEGER | Always 1; used as row count aggregate |
| `prelim_flag` | TEXT | `Y` = preliminary/unconfirmed data; `N` = confirmed |

### Visit_Type Mapping

| Code | Category | Sub-category |
|------|----------|-------------|
| `FV` | First Visit | In-person |
| `RV` | Repeat Visit | In-person |
| `FW` | First Visit | Walk-in |
| `RW` | Repeat Visit | Walk-in |
| `DF` | First Visit | Telehealth |
| `DR` | Repeat Visit | Telehealth |
| `FD` | First Visit | Telehealth (alt) |
| `RD` | Repeat Visit | Telehealth (alt) |
| `AF` | First Visit | Allied Health |
| `AR` | Repeat Visit | Allied Health |
| `PA` | — | Pre-Anaesthetic |
| `FS`/`RS` | — | Staff Clinic |

### Derived / Computed Fields (in reporting)
- **New/Repeat tag**: `FV/FW/DF/FD` → "New"; `RV/RW/DR/RD` → "Repeat"
- **Telehealth flag**: `DF/FD/DR/RD` → "TeleHealth"; others → "Not TeleHealth"
- **Age group**: 5-year buckets (1–4, 5–9, … 85+)
- **Pat_Class**: `A/B1` → "Private"; `B2/C` → "Subsidised"
- **Ref_Hosp_category**: GP | Polyclinic | NUH | Other inter-hospital | Intra-dept

---

## 2. Combined_UCC — Urgent Care Centre (A&E)

**Database table:** `urgentcarecenter`  
**Primary date column:** `Visit_Date`  
**Scripts:** `NGEMR_Procedure_SOC_UCC_Consolidated.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Attending physician | `Att_Phy_Name != 'CANCELLATION'` | Remove system-cancelled entries |
| Case end type | `Case_End_Type != 'Cancelled'` | Completed visits only |
| Date window | `Visit_Date` within 12-month rolling window | Standard reporting period |

### Exclusion Criteria
- `Att_Phy_Name == 'CANCELLATION'`
- `Case_End_Type == 'Cancelled'`

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Patient encounter number |
| `Visit_Date` | TIMESTAMP | Date of UCC attendance |
| `Visit_Time` | TIME | Time of UCC attendance |
| `Visit_Type` | TEXT | Visit classification (`EA`=Emergency Attendance) |
| `TrtOU` | TEXT | Treating OU (e.g. "Alex Urgent Care Centre") |
| `Clinical_Dept` | TEXT | Clinical department |
| `Sub-Specialty` | TEXT | Sub-specialty |
| `Case_End_Type` | TEXT | Discharge disposition — see mapping below |
| `Arrival_Mode` | TEXT | How patient arrived (`Walk In`, `Ambulance`, etc.) |
| `PACS` | TEXT | Patient Acuity Classification Score (triage acuity; P1–P5) |
| `PACS_Start_Date` | TIMESTAMP | When PACS triage began |
| `PACS_Start_Time` | TIME | PACS triage start time |
| `PACS_End_Date` | TIMESTAMP | When PACS triage ended |
| `PACS_End_Time` | TIME | PACS triage end time |
| `TRIAGE_ACUITY` | TEXT | Triage acuity level from NGEMR (preferred over PACS) |
| `CONSULT_ACUITY` | TEXT | Consult-assigned acuity (fallback if TRIAGE_ACUITY null) |
| `DoB` | TIMESTAMP | Patient date of birth |
| `Gender` | TEXT | Patient gender |
| `Race` | TEXT | Patient race |
| `Referral_type` | TEXT | Referral source type |
| `Att_Phy_MCR_No` | TEXT | Attending physician MCR number |
| `Att_Phy_Name` | TEXT | Attending physician name |
| `Pri_Diag_Code` | TEXT | Primary diagnosis code |
| `Pri_Diag_Desc` | TEXT | Primary diagnosis description |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID (links to other tables) |
| `ED_DEPARTURE_DTTM` | TIMESTAMP | Time patient left ED |
| `HOSPITAL_ADMISSION_DTTM` | TIMESTAMP | Time admitted to inpatient (if admitted) |
| `EVENT_ARRIVAL_TIME` | TIMESTAMP | Actual arrival timestamp |
| `TRIAGE_START_TIME` | TIMESTAMP | Triage initiation time |
| `TRIAGE_END_TIME` | TIMESTAMP | Triage completion time |
| `IP_BED_REQUEST_TIME` | TIMESTAMP | When inpatient bed was requested |
| `IP_ADMIT_TIME` | TIMESTAMP | When inpatient bed was assigned |
| `ED_DISCHARGE_TIME` | TIMESTAMP | ED discharge time |
| `FIRST_IP_ADM_OU` | TEXT | First inpatient ward admitted to |
| `cnt` | INTEGER | Always 1; row count |
| `prelim_flag` | TEXT | `Y`=preliminary; `N`=confirmed |

### Acuity Priority Logic
Triage acuity is resolved in priority order:
1. `TRIAGE_ACUITY` (from NGEMR)
2. `CONSULT_ACUITY` (if above null)
3. `PACS` (if both above null)

### Case_End_Type Mapping (NGEMR → DSA standard)

| NGEMR value | Standardised value |
|-------------|-------------------|
| `Patient discharged` | Discharged |
| `Discharged` | Discharged |
| `Admitted` | Admit |
| `Transfer to Other ED` | Transfer to Other ED |
| `Discharge to Community Hospital` | Discharge to Community Hosp |
| `AMA` / `AOR` | AMA/AOR |
| `Decant` | Decant |
| `Death` | Death |

### Key Derived Fields (in reporting)
- **Disposition group**: Admit vs Discharged vs Transfer vs AMA/AOR
- **Triage bucket**: P1 (resuscitation) → P5 (non-urgent)
- **Waiting time**: `TRIAGE_START_TIME - EVENT_ARRIVAL_TIME`
- **ED LOS**: `ED_DEPARTURE_DTTM - EVENT_ARRIVAL_TIME`
- **Door-to-doctor time**: `TRIAGE_END_TIME - EVENT_ARRIVAL_TIME`

---

## 3. Combined_adm — Inpatient Admissions

**Database table:** `admission`  
**Primary date column:** `Adm_Date`  
**Scripts:** `NGEMR_Inpatient_Reporting.py`, `data_prep.py`, `NGEMR_Convert_Dates.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Admission type | `Adm_Type` matches `EM|SD|DI|EL|TA|RA` | Inpatient admissions only |
| Status | `Adm_Status != 'P'` | Confirmed admissions only |
| Date window | `Adm_Date` within 12-month rolling window | Standard reporting period |

### Exclusion Criteria
- `Adm_Status == 'P'` — pending/pre-admission
- `Adm_Ward` in `['LWEDTU', 'LWASW', 'LWDSW', 'LWVOTU', 'LOMOT']` — EDTU, Ambulatory Surgery Ward, Day Surgery Ward, VOTU, MOT holding
- Admission types outside `[EM, SD, DI, EL, TA, RA]` (e.g. outpatient encounters)

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Inpatient episode number (`2800`/`8280` prefix) |
| `C` / `record_type` | TEXT | Record type flag (`E`=episode, `D`=discharge record) |
| `Adm_Date` | TIMESTAMP | Admission date |
| `Adm_Time` | TIME | Admission time |
| `Adm_Type` | TEXT | Admission route — see mapping below |
| `Adm_Dept_OU` | TEXT | Admitting department code |
| `Adm_Nrs_OU` | TEXT | Admitting nursing unit (ward) code |
| `Adm_Bed` | TEXT | Admitting bed code |
| `Adm_Cls` | TEXT | Patient class at admission (`A`, `B1`, `B2`, `C`) |
| `Adm_Acmd_Cat` | TEXT | Accommodation category (`A1`, `B1`, `B2`, `C`, `ICU`, `HD`, `ISO`) |
| `Adm_Trt_Cat` | TEXT | Treatment category code at admission |
| `Wish_Cls` | TEXT | Patient's requested class (may differ from assigned) |
| `Disch_Date` | TIMESTAMP | Discharge date (present in adm table as forward reference) |
| `Disch_Cls` | TEXT | Patient class at discharge |
| `Disch_Dept_OU` | TEXT | Discharging department |
| `Disch_Nrs_OU` | TEXT | Discharging ward |
| `Disch_Bed` | TEXT | Discharging bed |
| `Disch_Time` | TIME | Discharge time |
| `Disch_Type` | TEXT | Discharge type code |
| `Disch_Type_1` | TEXT | Discharge type description |
| `Disch_Phy` | TEXT | Discharging physician MCR |
| `Disch_Phy_Name` | TEXT | Discharging physician name |
| `Attn_Phy` | TEXT | Attending physician MCR |
| `Attn_Phy_Name` | TEXT | Attending physician name |
| `Age` | TEXT | Age at admission |
| `Sex` | TEXT | Patient sex |
| `Nationality` | TEXT | Nationality code |
| `Nationality_1` | TEXT | Nationality description |
| `Resident` | TEXT | Residency status (`Resident`, `PR`, etc.) |
| `Birthdate` | TIMESTAMP | Date of birth |
| `Diagnosis_Code` | TEXT | Principal diagnosis ICD code |
| `Diagnosis_Desc` | TEXT | Principal diagnosis description |
| `Prin_Diagnosis_Code` | TEXT | Refined principal diagnosis code (NGEMR) |
| `Prin_Diagnosis_Desc` | TEXT | Refined principal diagnosis description |
| `Adm_Reason` | TEXT | Reason for admission (e.g. `SOC`, `A&E`, `Others`) |
| `Ref_Hosp_1` | TEXT | Referring hospital/source code |
| `Referral_type` | TEXT | Referral type |
| `DRG_Code` | TEXT | Diagnosis-Related Group code |
| `DRG_Desc` | TEXT | DRG description |
| `Postal_Code` | TEXT | Patient postal code |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
| `cnt` | INTEGER | Always 1 |
| `prelim_flag` | TEXT | `Y`=preliminary; `N`=confirmed |

### Adm_Type Mapping

| Code | Meaning |
|------|---------|
| `EM` | Emergency admission (via A&E/UCC) |
| `EL` | Elective admission (planned) |
| `SD` | Same-day admission |
| `DI` | Direct admission (from clinic/GP) |
| `TA` | Transfer in (from another hospital) |
| `RA` | Readmission |

### Paying Status Logic
- `Adm_Cls` in `[B2, B2P, C]` → "Subsidised"
- All others → "Paying"

### Ward Exclusion Codes
| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Department Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU (not standard inpatient) |
| `LOMOT` | Main Operating Theatre holding |

---

## 4. Combined_disch — Inpatient Discharges

**Database table:** `discharge`  
**Primary date column:** `Disch_Date`  
**Scripts:** `NGEMR_Inpatient_Reporting.py`, `NGEMR_Convert_Dates.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Admission type | `Adm_Type` matches `EM|SD|DI|EL|TA|RA` | Inpatient episodes only |
| Status | `Disch_Status != 'P'` | Completed discharges only |
| Date window | `Disch_Date` within 12-month rolling window | Standard reporting period |

### Exclusion Criteria
- `Disch_Status == 'P'` — not yet discharged
- `Nrs_OU` in `['LWEDTU', 'LWASW', 'LWDSW', 'LWVOTU', 'LOMOT', 'LCUCC']` — non-standard inpatient wards
- Same admission type filter as `Combined_adm`

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Inpatient episode number |
| `C` / `record_type` | TEXT | Record type (`D`=discharge record) |
| `Adm_Date` | TIMESTAMP | Original admission date |
| `Adm_Time` | TIME | Admission time |
| `Adm_Type` | TEXT | Admission route (same codes as adm table) |
| `Adm_Class` | TEXT | Patient class at admission |
| `Adm_Dept_OU` | TEXT | Admitting department |
| `Disch_Date` | TIMESTAMP | Discharge date |
| `Disch_Time` | TIME | Discharge time |
| `Disch_Class` | TEXT | Patient class at discharge |
| `Disch_Dept_OU` | TEXT | Discharging department code |
| `Disch_Dept_OU_Text` | TEXT | Discharging department name |
| `Nrs_OU` | TEXT | Discharging nursing unit (ward) |
| `Disch_Nrs_OU_Text` | TEXT | Discharging ward name |
| `Discharge_Type_Text` | TEXT | Discharge disposition (e.g. "Discharged Home", "Death", "Transfer") |
| `Discharge_Physician` | TEXT | Discharging physician MCR |
| `Discharge_Physician_Name` | TEXT | Discharging physician name |
| `Disch_Reason` | TEXT | Discharge reason code |
| `Disch_Reason_Text` | TEXT | Discharge reason description |
| `Death_Date` | TIMESTAMP | Date of death (if applicable) |
| `Death_Time` | TIME | Time of death (if applicable) |
| `Discharge_w_in_24_hrs` | TEXT | Flag for discharge within 24 hours of admission |
| `LOS` | TEXT | Length of stay (days) |
| `Age` | TEXT | Age at discharge |
| `Sex` | TEXT | Patient sex |
| `Nationality` | TEXT | Nationality |
| `Trt_Cat` | TEXT | Treatment category at discharge |
| `Adm_Trt_Cat` | TEXT | Treatment category at admission |
| `Pri_Diag_Code` | TEXT | Principal discharge diagnosis ICD code |
| `Pri_Diag_Code_Text` | TEXT | Principal discharge diagnosis description |
| `Other_Diag_Code` | TEXT | Additional diagnosis codes (pipe-separated) |
| `DRG_Code` | TEXT | DRG code |
| `DRG_Desc` | TEXT | DRG description |
| `Referring_Hospital` | TEXT | Referring hospital code |
| `Referring_Hospital_Text` | TEXT | Referring hospital name |
| `Post_Discharge_Hospital_Text` | TEXT | Hospital patient transferred to (if applicable) |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
| `Postal` | TEXT | Patient postal code |
| `cnt` | INTEGER | Always 1 |
| `prelim_flag` | TEXT | `Y`=preliminary; `N`=confirmed |

### Key Derived Fields (in reporting)
- **death flag**: 1 if `Discharge_Type_Text` starts with "Death"
- **LOS**: Computed as `Disch_Date - Adm_Date` (days); same-day = 1
- **Class adjusted for ICU/ISO**: `HD/LW8/LW9` ward → ISO; ICU ward → ICU
- **Readmission within 30 days**: Join back to admission table on `Case_No`

---

## 5. Combined_inflight — Daily Inpatient Census (Patient Days)

**Database table:** `inflight`  
**Primary date column:** `Inflight_Date`  
**Scripts:** `NGEMR_Inpatient_Reporting.py`, `NGEMR_Convert_Dates.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Date window | `Inflight_Date` within 12-month rolling window | Standard reporting period |

### Exclusion Criteria
- `Ward` in `['LWEDTU', 'LWASW', 'LWDSW', 'LWVOTU', 'LOMOT', 'LCUCC']` — non-standard census wards
- For lodger reports: `Accom_Category` must be in `[A1, B1, B2]` AND `Class_abc` in `[B1, B2, C]` AND `Accom_Category != Class_abc`

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Inpatient episode number |
| `C` / `record_type` | TEXT | Record type (`J`=census/inflight record) |
| `Inflight_Date` | TIMESTAMP | Census snapshot date (the date this patient was occupying a bed) |
| `Admit_Date` | TIMESTAMP | Original admission date |
| `Ward` | TEXT | Ward code on the census date |
| `Bed` | TEXT | Bed code on the census date |
| `Dept_OU` | TEXT | Department organisational unit |
| `Attend_Phy` | TEXT | Attending physician name on this census day |
| `LOS` | TEXT | Length of stay as of census date |
| `Trt_Cat` | TEXT | Treatment category |
| `Class` | TEXT | Patient class |
| `Accom_Category` | TEXT | Actual accommodation category (`A1`, `B1`, `B2`, `C`, `ICU`, `HD`, `ISO`, `OTHER`) |
| `Age` | TEXT | Patient age |
| `Sex` | TEXT | Patient sex |
| `Diagnosis_Code` | TEXT | Primary diagnosis code |
| `Diagnosis_Desc` | TEXT | Primary diagnosis description |
| `Adm_Type` | TEXT | Original admission route |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
| `cnt` | INTEGER | Always 1; represents one patient-day |
| `prelim_flag` | TEXT | `Y`=preliminary; `N`=confirmed |

### Important Notes
- Each row represents **one patient-day** (one patient in one bed on one date)
- `SUM(cnt)` over a date range = total patient-days (occupancy metric)
- `COUNT(DISTINCT Case_No)` on a single `Inflight_Date` = census count for that day
- **Accom_Category `OTHER`**: Remapped to ward class (`Ward_cls`) from ward reference table — do not use raw "OTHER" value for patient class analysis
- **Lodger definition**: Patient whose `Accom_Category` differs from their entitled `Class` (e.g. B2 patient in A1 accommodation)

### Key Derived Fields (in reporting)
- **Patient-days by class**: Group by `Inflight_Date` + `Accom_Category`
- **Occupancy rate**: `SUM(patient-days) / (beds × days in period)`
- **Class_with_icu_iso**: Adjusted class accounting for ICU/ISO/HD stays

---

## 6. Combined_procedure — Surgical and Procedural Cases

**Database table:** `procedure`  
**Primary date column:** `Operation_Date`  
**Scripts:** `NGEMR_Procedure_SOC_UCC_Consolidated.py`, `data_prep.py`

### Inclusion Criteria
| Filter | Logic | Meaning |
|--------|-------|---------|
| Date window | `Operation_Date` within 12-month rolling window | Standard reporting period |
| Case prefix | `Case_No` starts with `2800` or `8280` | AH cases only |

### Exclusion Criteria
- Cases without valid `Case_No` prefix (non-AH institutions)
- Duplicate case removal: For OT procedures, deduplicate on `Case_No` (count episodes, not procedures)

### Segmentation by Admission Type

**Outpatient procedures** (`Adm_Type` in `[DS, ES, DO]`):
| Code | Meaning |
|------|---------|
| `DS` | Day Surgery |
| `ES` | Endoscopy Surgery (day) |
| `DO` | Day Outpatient (endoscopy/procedure clinic) |

**Inpatient procedures** (`Adm_Type` in `[DI, SD, EM, EL]`):
| Code | Meaning |
|------|---------|
| `DI` | Direct inpatient |
| `SD` | Same-day inpatient |
| `EM` | Emergency inpatient |
| `EL` | Elective inpatient |

**OT (Operating Theatre) procedures**: Subset where `Treatment_OU` in `['ALEX DAY SURGERY OT', 'ALEX MAIN OPERATING THEATRE']` AND `Adm_Type` not in `[DO, ES]`

### Key Columns

| Column | Type | Interpretation |
|--------|------|---------------|
| `Case_No` | TEXT | Case/episode number |
| `C` / `record_type` | TEXT | Record type (`G`=surgical record) |
| `UID` | TEXT | Unique procedure identifier (composite key) |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
| `Operation_Date` | TEXT | Date of operation/procedure |
| `OT_Begin_Date` | TIMESTAMP | OT session start date (from SAP) |
| `OT_Begin_Time` | TIME | OT session start time |
| `OT_End_Date` | TIMESTAMP | OT session end date |
| `OT_End_Time` | TIME | OT session end time |
| `Procedure_Start_Instant` | TEXT | NGEMR procedure start timestamp |
| `Procedure_End_Instant` | TEXT | NGEMR procedure end timestamp |
| `In_Procedure_Room` | TEXT | Time patient entered procedure room |
| `Out_of_Procedure_Room` | TEXT | Time patient left procedure room |
| `Surgical_Visit_Type` | TEXT | Surgery type (`Elective Oper`, `Emergency Oper`, etc.) |
| `Adm_Type` | TEXT | Admission type (determines OP/IP segmentation) |
| `OpTable` | TEXT | Operating table number; `M` = Minor surgical procedure |
| `Treatment_OU` | TEXT | Operating theatre location |
| `Treatment_Rm` | TEXT | Specific room name |
| `Sub-Specialty` | TEXT | Surgical sub-specialty |
| `Clinical_Dept` | TEXT | Clinical department |
| `Surgeon_MCR_No` | TEXT | Primary surgeon MCR |
| `Surgeon` | TEXT | Primary surgeon name |
| `Anaesthetist_MCR_No` | TEXT | Anaesthetist MCR |
| `Anaesthetist` | TEXT | Anaesthetist name |
| `First_Performing_Surgeon` | TEXT | NGEMR first performing surgeon |
| `First_Performing_Surgeon_MCR` | TEXT | NGEMR first performing surgeon MCR |
| `Performing_Surgeon` | TEXT | NGEMR performing surgeon |
| `Op_Code` | TEXT | SAP procedure code |
| `Proc_Code` | TEXT | NGEMR procedure code |
| `Proc_Description` | TEXT | NGEMR procedure description |
| `Surg_Cd_Description` | TEXT | SAP procedure description |
| `S_CODE` | TEXT | Surgical code |
| `TOSP_Level_Grouping` | TEXT | Table of Surgical Procedures level (complexity) |
| `DRG_Code` | TEXT | DRG code |
| `ASA_Score` | TEXT | ASA physical status score (anaesthetic risk) |
| `Surgery_Case_Type` | TEXT | Elective / Emergency |
| `Surgery_Case_Classification` | TEXT | Surgery classification |
| `Cls` | TEXT | Patient class |
| `Resident` | TEXT | Surgeon residency status |
| `Age` | TEXT | Patient age |
| `Sex` | TEXT | Patient sex |
| `Postal_Code` | TEXT | Patient postal code |
| `Panel_Number` | TEXT | Procedure panel number |
| `Proc_Row_Num` | TEXT | Row number within a case (for multi-procedure cases) |
| `cnt` | INTEGER | Always 1 |
| `prelim_flag` | TEXT | `Y`=preliminary; `N`=confirmed |
| `Period` | TEXT | Reporting period label (e.g. "Jan 2025") |

### Key Derived Fields (in reporting)
- **Surgery_Duration_Mins**: `(OT_End_Time - OT_Begin_Time) + 15 mins buffer`; or from NGEMR `Procedure_End - Procedure_Start`
- **OpTable categorisation**: First char of `OpTable`; `M` → "Minor Surgical Procedures"; numeric → OT table number
- **Sub-Specialty_Final**: Harmonised sub-specialty (e.g. "Fast/Chronic General Surgery" → "Alex General Surgery")
- **Pat_Class**: `A/B1` → "Private"; `B2/C` → "Subsidised"
- **Outpatient vs Inpatient split**: Determined by `Adm_Type` (see segmentation above)
- **Episode vs Procedure count**: `COUNT(DISTINCT Case_No)` = episodes; `COUNT(*)` = procedures (multi-procedure cases have multiple rows)

---

## Cross-Table Reference: Linking Keys

| Tables | Join Key | Notes |
|--------|----------|-------|
| `admission` ↔ `discharge` | `Case_No` | One-to-one per episode |
| `admission` ↔ `inflight` | `Case_No` | One-to-many (one row per patient-day) |
| `admission` ↔ `procedure` | `Case_No` or `PAT_ENC_CSN_ID` | One-to-many (multi-procedure cases) |
| `urgentcarecenter` ↔ `admission` | `PAT_ENC_CSN_ID` or `SAP_IP_CASE_NO` | Links A&E attendance to subsequent admission |
| `outpatient` ↔ `procedure` | `PAT_ENC_CSN_ID` | Links SOC visit to day surgery procedure |

---

## Common Flags & Status Codes

| Field | Value | Meaning |
|-------|-------|---------|
| `prelim_flag` | `Y` | Preliminary data — subject to change, exclude from official reports |
| `prelim_flag` | `N` | Confirmed/finalised data |
| `Status` / `Adm_Status` / `Disch_Status` | `P` | Pending — not yet completed, **exclude from all counts** |
| `Status` / `Adm_Status` / `Disch_Status` | `A` | Actual/confirmed — **include in counts** |
| `C` / `record_type` | `E` | Episode header record (admission) |
| `C` / `record_type` | `D` | Discharge record |
| `C` / `record_type` | `J` | Inflight/census record |
| `C` / `record_type` | `G` | Surgical/procedure record |

---

## Patient Class Reference

| Code | Class | Paying Status | Notes |
|------|-------|--------------|-------|
| `A` / `A1` | Class A | Paying | Full private |
| `B1` | Class B1 | Paying | Private, some subsidy |
| `B2` / `B2P` | Class B2 | Subsidised | Standard subsidy |
| `C` | Class C | Subsidised | Highest subsidy |
| `SUB` | Subsidised | Subsidised | SOC shorthand |
| `ISO` | Isolation | — | Accommodation type, not class |
| `ICU` | Intensive Care | — | Accommodation type |
| `HD` | High Dependency | — | Accommodation type |

---

*Generated from analysis of HIM reporting scripts. Last updated: 2026-07-03.*
