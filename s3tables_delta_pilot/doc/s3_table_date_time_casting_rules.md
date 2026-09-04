# Date and Time Casting Rules for S3 Tables

## Purpose

Define the standard casting rules used when ingesting source files into an Amazon S3 Table / Apache Iceberg table.

The uploader should infer temporal values from the source data and convert them into the appropriate target data type before writing to the table.

---

## Casting Rules

| Source value example | Recognised format | Target type | Stored value example |
|---|---|---|---|
| `20240513` | `YYYYMMDD` | `DATE` | `2024-05-13` |
| `2024-05-13` | `YYYY-MM-DD` | `DATE` | `2024-05-13` |
| `2024.05.13` | `YYYY.MM.DD` | `DATE` | `2024-05-13` |
| `2024-05-13 14:35:21` | `YYYY-MM-DD HH:mm:ss` | `TIMESTAMP` | `2024-05-13 14:35:21` |
| `14:35:21` | `HH:mm:ss` | `STRING` | `14:35:21` |

---

## Required Logic

### 1. Date-only values

The following formats must be recognised as dates:

```text
YYYYMMDD
YYYY-MM-DD
YYYY.MM.DD
```

Examples:

```text
20240513
2024-05-13
2024.05.13
```

All must be parsed and stored as:

```text
DATE → 2024-05-13
```

Do not store date-only values as `TIMESTAMP`.

---

### 2. Date + time values

Values containing both a valid date and time:

```text
YYYY-MM-DD HH:mm:ss
```

Example:

```text
2024-05-13 14:35:21
```

must be stored as:

```text
TIMESTAMP
```

---

### 3. Time-only values

Values containing only a time:

```text
HH:mm:ss
```

Example:

```text
14:35:21
```

must always remain:

```text
STRING
```

Do not create an artificial date in order to convert a time-only value into a timestamp.

---

## Validation Requirements

The ingestion process must validate the value before conversion.

Examples:

```text
20240229    → valid DATE
20240230    → invalid
20241301    → invalid
2024.05.13  → valid DATE
25:30:00    → invalid time
```

Do not rely on a blind database cast.

Instead:

1. Detect the expected format.
2. Parse the value using that format.
3. Validate that it represents a real calendar date/time.
4. Convert it to the target type.
5. If parsing fails, flag/reject the value according to the ingestion error-handling policy.

---

## Type Inference Priority

When inspecting a STRING column, evaluate temporal formats in this order:

```text
YYYY-MM-DD HH:mm:ss  → TIMESTAMP
YYYYMMDD             → DATE
YYYY-MM-DD           → DATE
YYYY.MM.DD           → DATE
HH:mm:ss             → STRING
otherwise            → STRING
```

The complete first file should be checked before establishing the initial S3 Table schema contract. Sample values shown in the UI are for review only and must not be the sole basis for inference.

---

## Expected Implementation Behaviour

```text
"20240513"             -> DATE("2024-05-13")
"2024-05-13"           -> DATE("2024-05-13")
"2024.05.13"           -> DATE("2024-05-13")
"20241304"             -> BIGINT("20241304")
"2024-05-13 14:35:21"  -> TIMESTAMP("2024-05-13 14:35:21")

"14:35:21"             -> STRING("14:35:21")
```

## Key Principle

Use the narrowest data type that accurately represents the source value:

- Date only → `DATE`
- Date and time → `TIMESTAMP`
- Time only → `STRING`
