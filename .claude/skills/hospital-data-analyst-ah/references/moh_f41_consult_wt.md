---
name: ah-analytics-f41
description: Column reference and transformation rules for Alexandra Hospital F41 QMS2.0 consultation waiting time reporting. Use when writing SQL or explaining F41 wait time, eligible consultation records, specialty and subsidy mappings, investigation flags, or the F41 report fields.
---

# AH Analytics — F41 Consultation Waiting Time

**One row per retained QMS2.0 consultation record. Primary date: `Appointment Date`.** The report measures minutes from the later of the appointment time and the start of consultation waiting to the start of service, floored at zero.

**Target indicators for `Wait Time (min)_new`:** Median ≤ 30 minutes; 95th percentile ≤ 60 minutes. Calculate both from the retained report records after all filters.

## Query baseline

Start with the QMS2.0 WTR MOH report data. The Python report script reads its Excel header at row 13 (`header=12`); it does not define a database table name. Apply the source filters, join the specialty lookup, derive the wait time, then apply the final filters. Use the actual warehouse table and date type supplied in the current environment.

## Filters used before deriving the report

Keep records only when:

```sql
"SAP Visit Type" IN (
  'First Visit', 'Repeat Visit',
  'Telehealth Doctor (First Visit) - Video',
  'Telehealth Doctor (Repeat Visit) – Video',
  'Telehealth Doctor (First Visit) - Phone',
  'Telehealth Doctor (Repeat Visit) - Phone'
)
AND "EPIC Specialty" IS NOT NULL
AND "Check-In Time" IS NOT NULL
AND "Start Serve Time for Consultation" >= "Start Wait Time for Consultation"
```

The repeat-visit video label contains an en dash (`–`), as in the source script. Parse `Appointment Date` as `%d/%m/%y` and the four clock columns as `%H:%M` if working from the same Excel export. The script compares clock times on the same parsed day; confirm source conventions before changing that assumption.

Join `EPIC Specialty` to `Dept OU` in `Class.xlsx`, sheet `F41_Map_Dept`, to get `Specialty Code`. Exclude records without a mapped `Specialty Code`. After computing `Wait Time (min)_new`, retain only values **at most 180 minutes**; missing values also fall out under the script's final comparison.

## Consultation wait calculation

Use these branches, in minutes, to match `Wait Time (min)_new`:

| Condition | Wait time |
|--------|--------|
| `Appointment Time` >= `Start Serve Time for Consultation` | `0` |
| Service starts after appointment, and `Start Wait Time for Consultation` < appointment | Service start minus appointment |
| Service starts after appointment, and start wait >= appointment | Service start minus start wait |

The source `Waiting Time` is converted to a separate `Wait Time (min)` column but is **not** the value used in the F41 output. In particular, the script's `.dt.minute` conversion of that source column is not a general duration conversion. Use `Wait Time (min)_new` for reporting.

## F41 report columns

| Column | Source or meaning |
|--------|--------|
| `Hospital` | Source hospital, passed through |
| `Report Quarter` | Source quarter, passed through |
| `Appointment Date` | Appointment date; primary reporting date |
| `Specialty Code` | Lookup value mapped from `EPIC Specialty` |
| `Subsidy Code` | `1` for `Subsidised` or `Subsidised for PR` level of care; `2` otherwise |
| `Appointment Time` | Scheduled clock time |
| `Check-In Time` | Patient check-in clock time; required |
| `Investigations` | `1` for listed pre-consultation services below; `2` otherwise |
| `Start Wait Time for Consultation` | Consultation queue start clock time |
| `Start Serve Time for Consultation` | Consultation service start clock time |
| `Wait Time (min)_new` | Recalculated consultation wait in minutes, capped by the 180-minute inclusion filter |

## Investigation service types

Set `Investigations = 1` when `Service type before Consultation` is one of `Audiology`, `BMD`, `FLS`, `Investigation`, `Laser`, `Mammogram`, `MRI`, `Refraction`, `Ultrasound`, `Visual Acuity`, or `X-ray`. Set it to `2` for every other value, including missing values.

The script writes these columns to the F41 workbook. Its output filename uses the quarter and year of the previous calendar month, while `Report Quarter` in the rows comes from the source data; do not substitute the filename's quarter for the row value.
