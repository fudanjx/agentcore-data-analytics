---
name: nuh-analytics-surgery
description: Column reference and SQL guidance for NUH surgery. Use when analyzing procedure volume, day surgery, normal delivery, inpatient surgery, emergency versus elective activity, or surgical paying status across 2023 to June 2026.
---

# NUH Analytics — surgery

**Count procedures, not cases or patients. Primary date: `SVISITDATE`. Coverage:
January 2023–June 2026.** Use the unified table with a half-open `SVISITDATE`
range. Never filter or group by `Period`, `Hosp_ABBR`, or `UID`.

`UID` is only the hybrid era discriminator: SAP is `UID IS NULL` through January
2024; Epic is `UID IS NOT NULL` from February 2024. Never infer the era from
`S_CODE` nullness.

## Locked hybrid surgical-category CASE

```sql
CASE
  WHEN "PATIENT_TYPE" = 'D' THEN 'Day Surgery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NULL
   AND "SUR_PROC_CODE" IN ('NSP836U','NSI836U','NSI038U','NSP038U')
    THEN 'Normal Delivery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NOT NULL
   AND "S_CODE" IN ('SP836U','SI836U','SI038U','SP038U')
    THEN 'Normal Delivery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NULL
   AND "SUR_PROC_CODE" NOT IN ('NSP836U','NSI836U','NSI038U','NSP038U')
    THEN 'Inpatient Surgery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NOT NULL
   AND "S_CODE" NOT IN ('SP836U','SI836U','SI038U','SP038U')
    THEN 'Inpatient Surgery'
  ELSE 'Unclassified'
END AS surgical_category
```

`Surgery_Patient_Class` is not valid for surgical category. Any unclassified row
is a QC failure. The SAP-only codes `NSI038U`/`NSP038U` and Epic-only
`SI038U`/`SP038U` are retained for future-proofing although none were observed.

## Emergency / elective and patient class

Use `"EMERG_IND" = 'X'` for Emergency and `"EMERG_IND" IS NULL` for Elective.
Do not use `EMERG_IND1` or `Surgery_Case_Type`.

For private/subsidised analysis, use `PATIENT_CLASS IN ('SP','RP','AC','AP','EP')`
as Private in CY2023; all other CY2023 values are Subsidised. From CY2024, use
`"Patient_Class_Grp" = 'Private'` as Private and all other values as Subsidised.

## Locked annual benchmarks

| Period | Day surgery | Normal delivery | Inpatient surgery | Total | Emergency |
|---|---:|---:|---:|---:|---:|
| CY2023 | 69,886 | 2,831 | 38,237 | 110,954 | 16,416 |
| CY2024 | 73,061 | 2,730 | 39,112 | 114,903 | 14,137 |
| CY2025 | 83,467 | 2,537 | 39,945 | 125,949 | 13,804 |
| H1 2026 | 42,210 | 1,146 | 20,716 | 64,072 | 7,560 |

QC: category total equals procedure total; Emergency plus Elective equals total;
monthly totals roll to annual total; and no `Unclassified` category exists. For
CY2025, Elective is 112,145. Investigate a Feb–Sep 2024 total below 7,000 as a
likely source-filter error.
