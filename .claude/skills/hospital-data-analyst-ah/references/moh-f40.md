---
name: ah-analytics-f40
description: Column reference and SQL guidance for the ah-analytics F40 first visit (FV) wait time table. Wait time is calculated using the difference between APPT CREATION DATE and SCHEDULED APPT DATE. Use when writing SQL to reference FV wait time at the median, 95th percentile, maximum wait time, or FV volume at Alexandra Hospital (AH).
---

# AH Analytics — F40 First Visit Wait Time Table

**One row per appointment. Primary date: `APPT CREATION DATE`.**

## Query baseline

Use the `f40` filters and canonical date in `references/data-ontology.yaml`.

## SQL filters used before deriving reports

```sql
WHERE "VISIT TYPE CODE" IN ('FV', 'DF', 'FD')
  AND "APPT STATUS DESC" NOT IN ('Cancelled')
  AND "APPT CREATION RATIONALE" IN ('Earliest Possible Appointment', 'Forcebook')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `EPIC CSN` | TEXT | Identifier |
| `GENDER` | TEXT | `M` / `F` |
| `RESIDENCY` | TEXT | Residency status — see mapping below |
| `VISIT TYPE CODE` | TEXT | Non-abbreviated column name is `VISIT TYPE DESC`. Key values to identify first visits are `FV`, `DF`, `FD` |
| `PATIENT CLASS CODE` | TEXT | Subsidy class of encounter. Regrouped in column `PRIVATE/SUBSIDIZED` |
| `APPT CREATION DATE` | DATE | Date when the appointment was created — primary date |
| `SCHEDULED APPT DATE` | DATE | Visit date of the appointment |
| `RESCHEDULED (Y/N)` | BOOLEAN | `Y` / `N` |
| `APPT STATUS DESC` | TEXT | Appointment status (i.e. whether the appointment was cancelled) |
| `APPT CREATION RATIONALE` | TEXT | Description of rationale for appointment creation |
| `REFERRAL TYPE DESC` | TEXT | Referral institution grouping |
| `REFERRAL HOSP DESC` | TEXT | Referral institution location |
| `TREATMENT OU CODE` | TEXT | Facility code where the appointment was scheduled — **do not use for reporting**. Use the corresponding non-abbreviated column `TREATMENT OU DESC` if required |
| `PERFORMING OU CODE` | TEXT | Specialty code where the appointment was scheduled — **do not use for reporting** |
| `PERFORMING OU VISIT TYPE + DESC` | TEXT | Concatenation of `PERFORMING OU VISIT TYPE` and `PERFORMING OU DESC`, delimited by ` + `. Remove all characters to the left of the delimiter and keep the text to the right. The text after the delimiter corresponds to `PERFORMING OU DESC`, which is directly mapped with `PERFORMING OU CODE` |
| `PERFORMING PHYSICIAN NAME` | TEXT | Attending physician name |
| `WAITING TIME` | INTEGER | Calculated appointment wait time from the difference between `APPT CREATION DATE` and `SCHEDULED APPT DATE` |
| `SPECIALTY OU VISIT TYPE + DESC` | TEXT | Name of appointment slot concatenated from `SPECIALTY OU VISIT TYPE` and `SPECIALTY OU VISIT TYPE DESC`, delimited by ` + `. Keep only the characters to the left of the delimiter |

## RESIDENCY codes

| Code | Meaning |
|------|---------|
| `SG` | Singaporean |
| `PR` | Permanent Resident |
| `FR` | Foreign Resident |
| `FNR` | Foreign Non-Resident |

## VISIT TYPE CODE codes

| Code | Meaning |
|------|---------|
| `FV` | First Visit |
| `DF` | Telehealth Doctor (First Visit) - Video |
| `FD` | Telehealth Doctor (First Visit) - Phone |
