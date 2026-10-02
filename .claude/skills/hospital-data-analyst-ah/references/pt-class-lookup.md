---
name: ah-analytics-pt-class-lookup
description: Shared patient class and residency lookup for all ah-analytics tables. Maps raw class codes to Class_abc (financial), Class_abc_MOH (MOH-facing), Resident_Type, and Resident_MOH. Referenced by outpatient, admission, discharge, inflight, and procedure.
---

# AH Analytics — Patient Class & Residency Lookup (`pt_class_abc`)

All five clinical tables (`outpatient`, `admission`, `discharge`, `inflight`, `procedure`) store a raw class code column (`Class`, `Adm_Cls`, `Disch_Class`, `Class`, `Cls` respectively). These must be resolved through this lookup before any class-based reporting.

## Full mapping table

| Raw code | `Class_abc` | `Class_abc_MOH` | `Resident_Type` | `Resident_MOH` |
|---|---|---|---|---|
| `A` | `A1` | `A1` | `SG` | `SG` |
| `AP` | `A1` | `A1` | `PR` | `PR` |
| `ARF` | `A1` | `A1` | `RF` | `FR` |
| `B1` | `B1` | `B1` | `SG` | `SG` |
| `B1P` | `B1` | `B1` | `PR` | `PR` |
| `B1RF` | `B1` | `A1` | `RF` | `FR` |
| `B2` | `B2` | `B2` | `SG` | `SG` |
| `B2P` | `B2` | `B2` | `PR` | `PR` |
| `B2RF` | `B2` | `A1` | `RF` | `FR` |
| `C` | `C` | `C` | `SG` | `SG` |
| `CP` | `C` | `C` | `PR` | `PR` |
| `CRF` | `C` | `A1` | `RF` | `FR` |
| `NR` | `A1` | `A1` | `NR` | `FNR` |
| `PTE` | `Private` | `Private` | `SG` | `SG` |
| `PTEP` | `Private` | `Private` | `PR` | `PR` |
| `PTRF` | `Private` | `Private` | `RF` | `FR` |
| `SUB` | `Subsidized` | `Subsidized` | `SG` | `SG` |
| `SUBP` | `Subsidized` | `Subsidized` | `PR` | `PR` |

## Class_abc vs Class_abc_MOH

`Class_abc_MOH` diverges from `Class_abc` only for the `*RF` (foreigner) variants of `B1`, `B2`, and `C`: production reclassifies all of them up to `A1` because foreigners are not entitled to subsidised tiers under MOH reporting rules.

- Use **`Class_abc`** for financial/internal reporting.
- Use **`Class_abc_MOH`** for MOH-facing outputs (e.g. F09/F04-style reports). Using the wrong one misstates subsidised vs. private counts specifically for foreigner patients.

## SQL

```sql
-- Class_abc (financial/internal)
CASE raw_class_col
  WHEN 'A'    THEN 'A1'          WHEN 'AP'   THEN 'A1'          WHEN 'ARF'  THEN 'A1'
  WHEN 'B1'   THEN 'B1'          WHEN 'B1P'  THEN 'B1'          WHEN 'B1RF' THEN 'B1'
  WHEN 'B2'   THEN 'B2'          WHEN 'B2P'  THEN 'B2'          WHEN 'B2RF' THEN 'B2'
  WHEN 'C'    THEN 'C'           WHEN 'CP'   THEN 'C'           WHEN 'CRF'  THEN 'C'
  WHEN 'NR'   THEN 'A1'
  WHEN 'PTE'  THEN 'Private'     WHEN 'PTEP' THEN 'Private'     WHEN 'PTRF' THEN 'Private'
  WHEN 'SUB'  THEN 'Subsidized'  WHEN 'SUBP' THEN 'Subsidized'
  ELSE raw_class_col
END AS "Class_abc"

-- Class_abc_MOH (MOH-facing — *RF variants of B1/B2/C forced to A1)
CASE raw_class_col
  WHEN 'A'    THEN 'A1'          WHEN 'AP'   THEN 'A1'          WHEN 'ARF'  THEN 'A1'
  WHEN 'B1'   THEN 'B1'          WHEN 'B1P'  THEN 'B1'          WHEN 'B1RF' THEN 'A1'
  WHEN 'B2'   THEN 'B2'          WHEN 'B2P'  THEN 'B2'          WHEN 'B2RF' THEN 'A1'
  WHEN 'C'    THEN 'C'           WHEN 'CP'   THEN 'C'           WHEN 'CRF'  THEN 'A1'
  WHEN 'NR'   THEN 'A1'
  WHEN 'PTE'  THEN 'Private'     WHEN 'PTEP' THEN 'Private'     WHEN 'PTRF' THEN 'Private'
  WHEN 'SUB'  THEN 'Subsidized'  WHEN 'SUBP' THEN 'Subsidized'
  ELSE raw_class_col
END AS "Class_abc_MOH"
```

Replace `raw_class_col` with the table-specific column name:
- `outpatient` → `"Class"`
- `admission` → `"Adm_Cls"`
- `discharge` → `"Disch_Class"` or `"Adm_Class"`
- `inflight` → `"Class"`
- `procedure` → `"Cls"`
