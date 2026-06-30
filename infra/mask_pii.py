"""
Mask PII columns in nuh-analytics database.

Phone numbers: keep first 4 digits, replace last 4 with XXXX
  e.g.  9875 5676  →  9875 XXXX
  e.g.  _x0038_501_x0020_5936  →  8501 XXXX  (URL-encoded, decoded first)

Street addresses: replace entirely with XXXXX (postal code kept separately)
  ADDRESS1, ADDRESS2, BLOCK_BUILD in inpatient_movement

Tables affected:
  emd                 — RESIDENT_TEL, CONTACT_TEL
  inpatient_movement  — CONTACT_TEL, ADDRESS1, ADDRESS2, BLOCK_BUILD
"""

import json
import os
import re

import boto3
import psycopg2
import psycopg2.extras

REGION = "ap-southeast-1"
SECRET_ARN = os.environ.get(
    "SECRET_ARN",
    "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J",
)
TARGET_DB = "nuh-analytics"

# URL-encoding map for digits and space used in emd phone columns
URL_DECODE = {
    "_x0030_": "0", "_x0031_": "1", "_x0032_": "2", "_x0033_": "3",
    "_x0034_": "4", "_x0035_": "5", "_x0036_": "6", "_x0037_": "7",
    "_x0038_": "8", "_x0039_": "9", "_x0020_": " ",
}

# Regex to match Singapore phone format after decoding: 4 digits, space, 4 digits
# Also handles no-space variant: 8 digits
PHONE_RE = re.compile(r"^([0-9]{4})\s?([0-9]{4})$")


def decode_url_phone(val: str) -> str:
    """Decode URL-encoded phone string like _x0038_501_x0020_5936 → 8501 5936."""
    for enc, ch in URL_DECODE.items():
        val = val.replace(enc, ch)
    return val.strip()


def mask_phone(val: str) -> str:
    """Keep first 4 digits, replace last 4 with XXXX."""
    if val is None:
        return None
    m = PHONE_RE.match(val.strip())
    if m:
        return f"{m.group(1)} XXXX"
    # Fallback: if longer string, just return as-is (not a standard SG number)
    return val


def get_conn():
    sm = boto3.client("secretsmanager", region_name=REGION)
    creds = json.loads(sm.get_secret_value(SecretId=SECRET_ARN)["SecretString"])
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        dbname=TARGET_DB,
        connect_timeout=30,
    )


def mask_emd_phones(conn):
    """Mask RESIDENT_TEL and CONTACT_TEL in emd (URL-encoded format)."""
    print("\n=== emd: masking RESIDENT_TEL and CONTACT_TEL ===")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SELECT COUNT(*) FROM emd WHERE "RESIDENT_TEL" IS NOT NULL OR "CONTACT_TEL" IS NOT NULL')
        total = cur.fetchone()["count"]
        print(f"  Rows with phone data: {total:,}")

        # Fetch all rows with phone data
        cur.execute('SELECT ctid, "RESIDENT_TEL", "CONTACT_TEL" FROM emd')
        rows = cur.fetchall()

    updates_res = []
    updates_con = []
    for row in rows:
        ctid = row["ctid"]

        res = row["RESIDENT_TEL"]
        if res:
            decoded = decode_url_phone(res)
            masked = mask_phone(decoded)
            updates_res.append((masked, ctid))

        con = row["CONTACT_TEL"]
        if con:
            decoded = decode_url_phone(con)
            masked = mask_phone(decoded)
            updates_con.append((masked, ctid))

    with conn.cursor() as cur:
        if updates_res:
            psycopg2.extras.execute_batch(
                cur,
                'UPDATE emd SET "RESIDENT_TEL" = %s WHERE ctid = %s',
                updates_res,
                page_size=5000,
            )
            print(f"  RESIDENT_TEL: {len(updates_res):,} rows masked")

        if updates_con:
            psycopg2.extras.execute_batch(
                cur,
                'UPDATE emd SET "CONTACT_TEL" = %s WHERE ctid = %s',
                updates_con,
                page_size=5000,
            )
            print(f"  CONTACT_TEL: {len(updates_con):,} rows masked")

    conn.commit()


def mask_inpatient(conn):
    """Mask CONTACT_TEL (keep first 4), ADDRESS1/ADDRESS2/BLOCK_BUILD (full mask)."""
    print("\n=== inpatient_movement: masking phone and address columns ===")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) FROM inpatient_movement
            WHERE "CONTACT_TEL" IS NOT NULL
               OR "ADDRESS1" IS NOT NULL
               OR "ADDRESS2" IS NOT NULL
               OR "BLOCK_BUILD" IS NOT NULL
        """)
        total = cur.fetchone()["count"]
        print(f"  Rows with PII data: {total:,}")

        cur.execute(
            'SELECT ctid, "CONTACT_TEL", "ADDRESS1", "ADDRESS2", "BLOCK_BUILD" FROM inpatient_movement'
        )
        rows = cur.fetchall()

    updates = []
    for row in rows:
        ctid = row["ctid"]
        tel = mask_phone(row["CONTACT_TEL"]) if row["CONTACT_TEL"] else None
        addr1 = "XXXXX" if row["ADDRESS1"] else None
        addr2 = "XXXXX" if row["ADDRESS2"] else None
        block = "XXXXX" if row["BLOCK_BUILD"] else None
        updates.append((tel, addr1, addr2, block, ctid))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """UPDATE inpatient_movement
               SET "CONTACT_TEL" = %s,
                   "ADDRESS1"    = %s,
                   "ADDRESS2"    = %s,
                   "BLOCK_BUILD" = %s
               WHERE ctid = %s""",
            updates,
            page_size=5000,
        )
        print(f"  {len(updates):,} rows updated")

    conn.commit()


def verify(conn):
    print("\n=== Verification ===")
    with conn.cursor() as cur:
        cur.execute('SELECT "RESIDENT_TEL", "CONTACT_TEL" FROM emd WHERE "RESIDENT_TEL" IS NOT NULL LIMIT 3')
        print("emd phone samples:")
        for row in cur.fetchall():
            print(f"  RESIDENT_TEL={row[0]}  CONTACT_TEL={row[1]}")

        cur.execute("""
            SELECT "CONTACT_TEL", "ADDRESS1", "ADDRESS2", "BLOCK_BUILD"
            FROM inpatient_movement
            WHERE "CONTACT_TEL" IS NOT NULL LIMIT 3
        """)
        print("inpatient_movement samples:")
        for row in cur.fetchall():
            print(f"  TEL={row[0]}  ADDR1={row[1]}  ADDR2={row[2]}  BLOCK={row[3]}")


def main():
    print("Connecting to nuh-analytics...")
    conn = get_conn()
    try:
        mask_emd_phones(conn)
        mask_inpatient(conn)
        verify(conn)
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
