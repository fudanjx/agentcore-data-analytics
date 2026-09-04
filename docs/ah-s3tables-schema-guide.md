# AH S3 Tables schema guide

> Live-schema snapshot generated on 2026-08-26 from the AH S3 Tables MCP endpoint. This guide is descriptive only; it makes no change to the Iceberg tables.

## Scope

- **Source / namespace:** `ah`
- **Tables:** 6
- **Schema source:** MCP `list_tables(source="ah")`
- **Nullability:** the schema listing exposes field names and types but does not include nullability. Use `describe_table` when a nullable/not-null check is required for a specific table.

## Type conventions

- `timestamp` represents a date or date-time field stored as a typed timestamp. Where a paired `*_time` field is typed as `string`, keep the pair together when reconstructing an event time.
- `string` includes coded values, identifiers, names, free text, and several numeric-looking fields such as age, LOS, and row counts. Cast only after validating the values.
- `bigint` is used for `cnt`, the native numeric record/count field.
- Fields such as `case_no`, `pat_enc_csn_id`, `ext_pat_id`, and `ed_episode_id` are identifiers, not quantities: preserve them as strings.

## Table overview

| Table | Columns | Timestamp columns | Primary content |
|---|---:|---:|---|
| `admission` | 65 | 3 | Inpatient admission and discharge-at-a-glance records, including admitting/discharging location, class, diagnoses, and attending details. |
| `discharge` | 96 | 4 | Detailed discharged-encounter records, including admission/discharge workflow, disposition, diagnoses, DRG, and patient attributes. |
| `inflight` | 26 | 2 | Point-in-time inpatient census / in-flight stay records, keyed to the reporting `inflight_date`. |
| `outpatient` | 51 | 4 | Outpatient visit and appointment records, including treating unit, clinic/specialty, referral, diagnoses, and appointment lifecycle. |
| `procedure` | 143 | 39 | Surgical/procedural encounters, including operating-theatre workflow, scheduling, clinicians, procedure attributes, and perioperative timestamps. |
| `urgentcarecenter` | 71 | 26 | Urgent Care Centre / emergency encounter records, including arrival, triage, clinical disposition, and ED/EDTU/IP workflow timestamps. |

## Cross-table guidance

- `case_no` is the clearest recurring encounter/case identifier, but it is not a guaranteed universal one-to-one key across clinical domains or reporting extracts.
- `pat_enc_csn_id` occurs in most tables; its population varies by source era, so do not assume it is complete.
- `prelim_flag` identifies preliminary reporting status where present. Apply it deliberately in metrics rather than filtering it implicitly.
- Several legacy and newer extracts coexist. Similar concepts may appear under different names (for example, `sex` / `gender`, `patient_name` / `pat_name` / `name`, and `adm_class` / `adm_cls`).
- `discharge` intentionally retains both its detailed discharge fields and legacy-compatible fields; avoid a broad `SELECT *` in downstream analytical models.

## `admission`

Inpatient admission and discharge-at-a-glance records, including admitting/discharging location, class, diagnoses, and attending details.

**Columns:** 65; **timestamp columns:** 3.

| Column | S3 Tables type |
|---|---|
| `case_no` | `string` |
| `record_type` | `string` |
| `pat_id` | `string` |
| `pat_name` | `string` |
| `resident` | `string` |
| `nationality` | `string` |
| `ext_pat_id` | `string` |
| `disch_date` | `timestamp` |
| `adm_dept_ou` | `string` |
| `adm_nrs_ou` | `string` |
| `adm_bed` | `string` |
| `current_ward` | `string` |
| `current_bed` | `string` |
| `adm_type` | `string` |
| `adm_src_1` | `string` |
| `age` | `string` |
| `adm_time` | `string` |
| `sex` | `string` |
| `adm_cls` | `string` |
| `adm_date` | `timestamp` |
| `wish_cls` | `string` |
| `adm_trt_cat` | `string` |
| `adm_acmd_cat` | `string` |
| `disch_cls` | `string` |
| `disch_dept_ou` | `string` |
| `disch_acmd_cat` | `string` |
| `disch_nrs_ou` | `string` |
| `disch_bed` | `string` |
| `attn_phy` | `string` |
| `birthdate` | `timestamp` |
| `country` | `string` |
| `diagnosis_code` | `string` |
| `diagnosis_desc` | `string` |
| `disch_time` | `string` |
| `disch_type` | `string` |
| `disch_type_1` | `string` |
| `disch_phy` | `string` |
| `disch_phy_name` | `string` |
| `drg_code` | `string` |
| `drg_desc` | `string` |
| `infect_dis` | `string` |
| `nationality_1` | `string` |
| `postal_code` | `string` |
| `r_n` | `string` |
| `attn_phy_name` | `string` |
| `adm_status` | `string` |
| `disch_status` | `string` |
| `ref_hosp_1` | `string` |
| `cnt` | `bigint` |
| `referral_type` | `string` |
| `referral_hospital` | `string` |
| `prelim_flag` | `string` |
| `pat_enc_csn_id` | `string` |
| `subvention_doc_type` | `string` |
| `residency` | `string` |
| `adm_reason` | `string` |
| `prin_diagnosis_code` | `string` |
| `prin_diagnosis_desc` | `string` |
| `diagnosis_group_id` | `string` |
| `diagnosis_group` | `string` |
| `adm_phy` | `string` |
| `adm_phy_name` | `string` |
| `prelim` | `string` |
| `drg_mpi_code` | `string` |
| `drg_name` | `string` |

## `discharge`

Detailed discharged-encounter records, including admission/discharge workflow, disposition, diagnoses, DRG, and patient attributes.

**Columns:** 96; **timestamp columns:** 4.

| Column | S3 Tables type |
|---|---|
| `adm_bed` | `string` |
| `adm_class` | `string` |
| `adm_date` | `timestamp` |
| `adm_dept_ou` | `string` |
| `adm_dept_ou_text` | `string` |
| `adm_nrs_ou` | `string` |
| `adm_nrs_ou_text` | `string` |
| `adm_physician` | `string` |
| `adm_room` | `string` |
| `adm_status` | `string` |
| `adm_status_text` | `string` |
| `adm_time` | `string` |
| `adm_trt_cat` | `string` |
| `adm_type` | `string` |
| `admitting_physician_name` | `string` |
| `age` | `string` |
| `attending_physician_name` | `string` |
| `attn_physician` | `string` |
| `record_type` | `string` |
| `case_no` | `string` |
| `country` | `string` |
| `death_date` | `timestamp` |
| `death_time` | `string` |
| `dept_ou` | `string` |
| `disch_bed` | `string` |
| `disch_class` | `string` |
| `disch_date` | `timestamp` |
| `disch_dept_ou_text` | `string` |
| `disch_nrs_ou_text` | `string` |
| `disch_reason` | `string` |
| `disch_reason_text` | `string` |
| `disch_room` | `string` |
| `disch_status` | `string` |
| `disch_time` | `string` |
| `disch_type` | `string` |
| `discharge_acuity_level` | `string` |
| `discharge_physician` | `string` |
| `discharge_physician_name` | `string` |
| `discharge_type_text` | `string` |
| `discharge_w_in_24_hrs` | `string` |
| `drg_code` | `string` |
| `drg_desc` | `string` |
| `ext_pat_no` | `string` |
| `full_name` | `string` |
| `los` | `string` |
| `nrs_ou` | `string` |
| `patient_id` | `string` |
| `physical_adm_date` | `timestamp` |
| `post_discharge_hospital_text` | `string` |
| `postal` | `string` |
| `pri_diag_code` | `string` |
| `pri_diag_code_text` | `string` |
| `r_n` | `string` |
| `referral_type` | `string` |
| `referring_hospital` | `string` |
| `referring_hospital_text` | `string` |
| `sex` | `string` |
| `cnt` | `bigint` |
| `trt_cat` | `string` |
| `prelim_flag` | `string` |
| `pat_id` | `string` |
| `pat_name` | `string` |
| `resident` | `string` |
| `nationality` | `string` |
| `ext_pat_id` | `string` |
| `current_ward` | `string` |
| `current_bed` | `string` |
| `adm_src_1` | `string` |
| `adm_cls` | `string` |
| `wish_cls` | `string` |
| `adm_acmd_cat` | `string` |
| `disch_cls` | `string` |
| `disch_dept_ou` | `string` |
| `disch_acmd_cat` | `string` |
| `disch_nrs_ou` | `string` |
| `attn_phy` | `string` |
| `birthdate` | `string` |
| `diagnosis_code` | `string` |
| `diagnosis_desc` | `string` |
| `disch_type_1` | `string` |
| `disch_phy` | `string` |
| `disch_phy_name` | `string` |
| `infect_dis` | `string` |
| `nationality_1` | `string` |
| `postal_code` | `string` |
| `attn_phy_name` | `string` |
| `ref_hosp_1` | `string` |
| `referral_type_1` | `string` |
| `referral_hospital` | `string` |
| `pat_enc_csn_id` | `string` |
| `adm_nurs_ou` | `string` |
| `adm_nurs_ou_text` | `string` |
| `other_diag_code` | `string` |
| `other_diag_code_text` | `string` |
| `drg_mpi_code` | `string` |
| `drg_name` | `string` |

## `inflight`

Point-in-time inpatient census / in-flight stay records, keyed to the reporting `inflight_date`.

**Columns:** 26; **timestamp columns:** 2.

| Column | S3 Tables type |
|---|---|
| `bed` | `string` |
| `pat_name` | `string` |
| `ward` | `string` |
| `dept_ou` | `string` |
| `ext_pat_id` | `string` |
| `los` | `string` |
| `admit_date` | `timestamp` |
| `inflight_date` | `timestamp` |
| `attend_phy` | `string` |
| `diagnosis_code` | `string` |
| `diagnosis_desc` | `string` |
| `age` | `string` |
| `trt_cat` | `string` |
| `accom_category` | `string` |
| `sex` | `string` |
| `class` | `string` |
| `case_no` | `string` |
| `adm_type` | `string` |
| `cnt` | `bigint` |
| `prelim_flag` | `string` |
| `record_type` | `string` |
| `pri_diagnosis_code` | `string` |
| `pri_diagnosis_desc` | `string` |
| `sec_diagnosis_code` | `string` |
| `sec_diagnosis_desc` | `string` |
| `pat_enc_csn_id` | `string` |

## `outpatient`

Outpatient visit and appointment records, including treating unit, clinic/specialty, referral, diagnoses, and appointment lifecycle.

**Columns:** 51; **timestamp columns:** 4.

| Column | S3 Tables type |
|---|---|
| `case_no` | `string` |
| `trt_ou` | `string` |
| `name` | `string` |
| `nationality` | `string` |
| `ext_pat_id` | `string` |
| `race` | `string` |
| `sex` | `string` |
| `age` | `string` |
| `attn_mcr` | `string` |
| `attn_phy` | `string` |
| `visit_type` | `string` |
| `movement_creation_date` | `timestamp` |
| `visit_date` | `timestamp` |
| `visit_time` | `string` |
| `trt_ou_id` | `string` |
| `visit_no` | `string` |
| `class` | `string` |
| `clinical_dept_id` | `string` |
| `clinical_dept` | `string` |
| `sub_specialty_id` | `string` |
| `sub_specialty` | `string` |
| `postal_code` | `string` |
| `comments` | `string` |
| `ref_hosp_address` | `string` |
| `trt_cat` | `string` |
| `trt_room` | `string` |
| `trt_room_name` | `string` |
| `ref_mcr` | `string` |
| `ref_phy` | `string` |
| `pri_diag_code` | `string` |
| `pri_diag_desc` | `string` |
| `status` | `string` |
| `cnt` | `bigint` |
| `referral_type` | `string` |
| `referral_hospital` | `string` |
| `prelim_flag` | `string` |
| `pat_enc_csn_id` | `string` |
| `visit_type_desc` | `string` |
| `prc_desc` | `string` |
| `appt_time` | `string` |
| `appt_creation_date` | `timestamp` |
| `appt_wt` | `string` |
| `adt_pat_class` | `string` |
| `appt_status` | `string` |
| `other_diag_code` | `string` |
| `other_diag_desc` | `string` |
| `diag_code_type_all` | `string` |
| `appt_request_dttm` | `timestamp` |
| `pat_pref_institution` | `string` |
| `prc_sub_specialty` | `string` |
| `appt_creation_rationale` | `string` |

## `procedure`

Surgical/procedural encounters, including operating-theatre workflow, scheduling, clinicians, procedure attributes, and perioperative timestamps.

**Columns:** 143; **timestamp columns:** 39.

| Column | S3 Tables type |
|---|---|
| `case_no` | `string` |
| `record_type` | `string` |
| `patient_name` | `string` |
| `ext_pat_id` | `string` |
| `clinical_dept` | `string` |
| `operation_date` | `timestamp` |
| `resident` | `string` |
| `sub_specialty` | `string` |
| `sub_spec` | `string` |
| `sex` | `string` |
| `age` | `string` |
| `cls` | `string` |
| `adm_type` | `string` |
| `surgical_visit_type` | `string` |
| `optable` | `string` |
| `visit_type` | `string` |
| `op_code` | `string` |
| `treatment_ou` | `string` |
| `room` | `string` |
| `treatment_rm` | `string` |
| `clinic` | `string` |
| `asa_score` | `string` |
| `surgeon_mcr_no` | `string` |
| `surgeon` | `string` |
| `anaesthetist_mcr_no` | `string` |
| `anaesthetist` | `string` |
| `surg_cd_description` | `string` |
| `drg_code` | `string` |
| `surgery_type` | `string` |
| `trtment` | `string` |
| `ot_begin_date` | `timestamp` |
| `ot_begin_time` | `string` |
| `ot_end_date` | `timestamp` |
| `ot_end_time` | `string` |
| `cnt` | `bigint` |
| `referral_type` | `string` |
| `referral_hospital` | `string` |
| `prelim_flag` | `string` |
| `period` | `string` |
| `hosp_abbr` | `string` |
| `uid` | `string` |
| `admsn_csn` | `string` |
| `pat_enc_csn_id` | `string` |
| `bill_num` | `string` |
| `admission_ward` | `string` |
| `treatment_ward` | `string` |
| `admsn_specialty` | `string` |
| `disch_specialty` | `string` |
| `patient_class` | `string` |
| `level_of_care` | `string` |
| `paying_pat_class_grp` | `string` |
| `admission_type` | `string` |
| `admission_reason_1` | `string` |
| `admsn_referral_source` | `string` |
| `accident_type` | `string` |
| `admitting_clinician` | `string` |
| `attending_clinician` | `string` |
| `ed_arrival_instant` | `timestamp` |
| `hsp_admsn_instant` | `timestamp` |
| `hsp_ip_admsn_instant` | `timestamp` |
| `hsp_disch_instant` | `timestamp` |
| `disch_clinician` | `string` |
| `los_days` | `string` |
| `disch_disposition` | `string` |
| `hsp_discharged_to` | `string` |
| `sg_doc_id` | `string` |
| `pat_dob` | `timestamp` |
| `race` | `string` |
| `nationality` | `string` |
| `subvention_doc_type` | `string` |
| `postal_code` | `string` |
| `case_order_id` | `string` |
| `surgery_case_type` | `string` |
| `surgery_priority` | `string` |
| `surgery_case_classification` | `string` |
| `surgery_patient_class` | `string` |
| `panel_number` | `string` |
| `proc_row_num` | `string` |
| `proc_code` | `string` |
| `proc_description` | `string` |
| `proc_laterality` | `string` |
| `proc_body_region` | `string` |
| `tosp_level_grouping` | `string` |
| `moh_ds_procedure_y_n` | `string` |
| `consultant_specialty` | `string` |
| `first_performing_surgeon` | `string` |
| `first_performing_surgeon_mcr` | `string` |
| `first_performing_surgeon_specialty` | `string` |
| `anaesthetist_specialty` | `string` |
| `case_order_created_date` | `timestamp` |
| `requested_date` | `timestamp` |
| `first_scheduled_instant` | `timestamp` |
| `last_scheduled_instant` | `timestamp` |
| `initial_scheduling_user` | `string` |
| `latest_scheduling_user` | `string` |
| `first_available_used` | `string` |
| `surgery_preference` | `string` |
| `waiting_time_days` | `string` |
| `projected_case_start_instant` | `timestamp` |
| `projected_case_end_instant` | `timestamp` |
| `procedure_start_instant` | `timestamp` |
| `procedure_end_instant` | `timestamp` |
| `asa_rating` | `string` |
| `in_pre_procedure_care` | `timestamp` |
| `pre_procedure_care_complete` | `timestamp` |
| `called_for` | `timestamp` |
| `in_block_area` | `timestamp` |
| `out_of_block_area` | `timestamp` |
| `sent_for` | `timestamp` |
| `in_ot_reception` | `timestamp` |
| `in_procedure_room` | `timestamp` |
| `surgical_prep_start` | `timestamp` |
| `sedation_start` | `timestamp` |
| `anaesthesia_start` | `timestamp` |
| `anaesthesia_induction` | `string` |
| `anaesthesia_ready` | `timestamp` |
| `prepare_pacu_bed` | `timestamp` |
| `anaesthesia_finish` | `timestamp` |
| `out_of_procedure_room` | `timestamp` |
| `clean_up_complete` | `timestamp` |
| `in_pacu` | `timestamp` |
| `pacu_care_complete` | `timestamp` |
| `out_of_pacu` | `timestamp` |
| `in_amb_unit_post_op_in_recovery` | `timestamp` |
| `amb_unit_post_op_complete_recovery_care_complete` | `timestamp` |
| `out_of_amb_unit_out_of_recovery` | `timestamp` |
| `procedural_care_complete` | `timestamp` |
| `is_cancerous_patient?` | `string` |
| `unscheduled_return_to_ot` | `string` |
| `ngemr_surgery_case_specialty` | `string` |
| `surgery_case_specialty_code` | `string` |
| `admsn_specialty_code` | `string` |
| `admsn_subspecialty` | `string` |
| `admsn_subspecialty_code` | `string` |
| `anaesthesia_type` | `string` |
| `disch_specialty_code` | `string` |
| `disch_subspecialty` | `string` |
| `disch_subspecialty_code` | `string` |
| `performing_ou` | `string` |
| `performing_surgeon` | `string` |
| `performing_surgeon_mcr` | `string` |
| `performing_surgeon_specialty` | `string` |
| `treatment_ou_1` | `string` |

## `urgentcarecenter`

Urgent Care Centre / emergency encounter records, including arrival, triage, clinical disposition, and ED/EDTU/IP workflow timestamps.

**Columns:** 71; **timestamp columns:** 26.

| Column | S3 Tables type |
|---|---|
| `case_no` | `string` |
| `trtou` | `string` |
| `trt_cat` | `string` |
| `clinical_dept_code` | `string` |
| `clinical_dept` | `string` |
| `sub_specialty_code` | `string` |
| `sub_specialty` | `string` |
| `visit_type` | `string` |
| `visit_date` | `timestamp` |
| `visit_time` | `string` |
| `name` | `string` |
| `ext_pat_id` | `string` |
| `gender` | `string` |
| `dob` | `timestamp` |
| `race` | `string` |
| `arrival_mode` | `string` |
| `referral_type` | `string` |
| `referral_hospital` | `string` |
| `pacs` | `string` |
| `pacs_start_date` | `timestamp` |
| `pacs_start_time` | `string` |
| `pacs_end_date` | `timestamp` |
| `pacs_end_time` | `string` |
| `trauma` | `string` |
| `trauma_start_date` | `timestamp` |
| `trauma_start_time` | `string` |
| `trauma_end_date` | `timestamp` |
| `trauma_end_time` | `string` |
| `case_end_type_code` | `string` |
| `row_cnt` | `string` |
| `case_end_type` | `string` |
| `att_phy_mcr_no` | `string` |
| `att_phy_name` | `string` |
| `gp_referral_address` | `string` |
| `remarks` | `string` |
| `pri_diag_code` | `string` |
| `pri_diag_desc` | `string` |
| `cnt` | `bigint` |
| `prelim_flag` | `string` |
| `pat_enc_csn_id` | `string` |
| `sap_ip_case_no` | `string` |
| `pat_age` | `string` |
| `subvention_doc_type` | `string` |
| `residency` | `string` |
| `ed_departure_dttm` | `timestamp` |
| `consult_acuity` | `string` |
| `ed_episode_id` | `string` |
| `hospital_admission_dttm` | `timestamp` |
| `diagnosis_code_billref` | `string` |
| `ed_disposition_dttm` | `timestamp` |
| `event_arrival_time` | `timestamp` |
| `triage_start_time` | `timestamp` |
| `triage_end_time` | `timestamp` |
| `edtu_bed_request_time` | `timestamp` |
| `edtu_admit_time` | `timestamp` |
| `edtu_order_br_time` | `timestamp` |
| `edtu_order_noted_time` | `timestamp` |
| `edtu_order_assigned_time` | `timestamp` |
| `edtu_order_completed_time` | `timestamp` |
| `ip_bed_request_time` | `timestamp` |
| `ip_admit_time` | `timestamp` |
| `ip_order_br_time` | `timestamp` |
| `ip_order_noted_time` | `timestamp` |
| `ip_order_assigned_time` | `timestamp` |
| `ip_order_completed_time` | `timestamp` |
| `ed_discharge_time` | `timestamp` |
| `ed_departure_time` | `timestamp` |
| `ed_lodger_flag` | `string` |
| `first_ip_adm_ou` | `string` |
| `first_ip_adm_bed` | `string` |
| `triage_acuity` | `string` |
