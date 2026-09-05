"""Healthcare-upload sanitization before S3 staging.

The policy is the union of the AH and NUH reference scripts, with the approved
unified rules: names are removed and postal codes retain their first two digits.
No plaintext, encryption key, or encrypted value is emitted in audit data.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

import boto3
import pandas as pd
import pyarrow as pa
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:data-insight-etl-encryption-ARn4mo"
SECRET_NAME = "data-insight-etl-encryption"
ENCRYPTED_PREFIX = "enc:v1:"
NRIC_PATTERN = re.compile(r"^[STFGM][0-9]{7}[A-Z]$", re.IGNORECASE)

AH_NAME_COLUMNS = {"PATIENT_NAME", "PAT_NAME", "FULL_NAME", "NAME"}
AH_IDENTIFIER_COLUMNS = {"EXT_PAT_ID", "EXT_PAT_NO"}


@dataclass(frozen=True)
class SanitizationPlan:
    drop_columns: tuple[str, ...]
    identifier_columns: tuple[str, ...]
    postal_columns: tuple[str, ...]
    age_columns: tuple[str, ...]


@dataclass(frozen=True)
class EncryptionMaterial:
    aes_key: bytes
    iv: bytes


def norm_col(col: object) -> str:
    text = str(col).strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _has_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _is_patient_name(name: str) -> bool:
    return norm_col(name) in AH_NAME_COLUMNS | {
        "PATIENT_NM", "PATIENT_FULL_NAME", "NAME_OF_PATIENT",
    }


def _is_dob(name: str) -> bool:
    value = norm_col(name)
    return value in {"DOB", "DATE_BIR", "DATE_OF_BIRTH", "BIRTH_DATE", "PATIENT_DOB"} or "BIRTH" in value


def _is_phone(name: str) -> bool:
    value = norm_col(name)
    if "TELEHEALTH" in value:
        return False
    return value.endswith("_TEL") or value in {"TEL", "RESIDENT_TEL", "CONTACT_TEL", "OFFICE_TEL", "MOBILE", "PHONE"} or _has_any(value, ["PHONE", "MOBILE", "HANDPHONE", "PAGER", "FAX", "CONTACT_NO", "TEL_NO"])


def _is_home_address(name: str, all_names: set[str]) -> bool:
    value = norm_col(name)
    if _has_any(value, ["ADDRESS", "STREET", "BUILDING_NAME", "HOME_ADDR", "HOME_ADDRESS"]) or value in {"CITY", "COUNTRY_ADDRESS"}:
        return True
    has_address_context = any(_has_any(item, ["ADDRESS", "POSTAL", "BUILDING_NAME", "STREET"]) for item in all_names)
    return has_address_context and value in {"BLOCK", "FLOOR", "UNIT", "UNIT_ROOM", "BLOCK_BUILD"}


def _is_postal(name: str) -> bool:
    value = norm_col(name)
    return "POSTAL" in value or value in {"ZIP", "ZIP_CODE"}


def _is_identifier(name: str) -> bool:
    value = norm_col(name)
    exact = {
        "HRN", "PREVIOUS_HRN", "PAT_ID", "PATIENT_ID", "PATIENT_ID_NO", "PATIENT_MRN",
        "PAT_MRN_ID", "PATIENT_MRN_ID", "PAT_MRN", "PAT_ENC_CSN_ID", "EPIC_CSN", "SAP_CSN",
        "ADMSN_CSN", "SURGERY_CSN", "CSN", "CASE_NO", "CASE_NOS", "KEY_CASE_NO1",
        "KEY_CASE_NO2", "CASE_ORDER_ID", "UID", "ACCT_N", "BILL_NUM", "SG_DOC_ID",
        "SUBVENT_NO", "SUBVENTION_DOC_NO", "SUBVENT_DOC_NO",
    }
    if value in exact | AH_IDENTIFIER_COLUMNS or value.endswith("_HRN") or "HRN" in value or "CSN" in value:
        return True
    if "CASE_NO" in value or value == "CASE_ORDER_ID" or value.endswith("_CASE_ORDER_ID"):
        return True
    tokens = set(value.split("_"))
    return ("PATIENT" in tokens and bool({"ID", "MRN"} & tokens)) or value.startswith("PAT_ID") or value.startswith("PAT_MRN")


def plan_for_columns(columns: Iterable[str]) -> SanitizationPlan:
    names = list(columns)
    normalized = {norm_col(name) for name in names}
    drop, identifiers, postal, age = [], [], [], []
    for name in names:
        if _is_patient_name(name) or _is_dob(name) or _is_phone(name) or _is_home_address(name, normalized):
            drop.append(name)
        elif _is_identifier(name):
            identifiers.append(name)
        elif _is_postal(name):
            postal.append(name)
        elif norm_col(name) == "AGE":
            age.append(name)
    return SanitizationPlan(tuple(drop), tuple(identifiers), tuple(postal), tuple(age))


def sanitised_schema(schema: pa.Schema, additional_encrypted: Iterable[str] = ()) -> tuple[pa.Schema, SanitizationPlan]:
    plan = plan_for_columns(schema.names)
    identifiers = set(plan.identifier_columns) | (set(additional_encrypted) - set(plan.drop_columns))
    postal, age = set(plan.postal_columns), set(plan.age_columns)
    fields = []
    for field in schema:
        if field.name in plan.drop_columns:
            continue
        data_type = pa.string() if field.name in identifiers | postal | age else field.type
        fields.append(pa.field(field.name, data_type, nullable=True, metadata=field.metadata))
    return pa.schema(fields, metadata=schema.metadata), plan


def detect_nric_columns(table: pa.Table, seed: str, sample_size: int = 5, threshold: int = 3) -> tuple[tuple[str, ...], dict[str, dict[str, int | bool]]]:
    """Find likely Singapore NRIC columns from deterministic safe samples.

    This is deliberately a sampled heuristic, not a checksum validator. It
    reports only counts/decisions, never the candidate values themselves.
    """
    _, plan = sanitised_schema(table.schema)
    excluded = set(plan.drop_columns) | set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
    detected, details = [], {}
    for field in table.schema:
        if field.name in excluded or not (pa.types.is_string(field.type) or pa.types.is_large_string(field.type)):
            continue
        values = table[field.name].to_pylist()
        nonempty = [str(value).strip() for value in values if value is not None and str(value).strip()]
        if not nonempty:
            continue
        sample = random.Random(f"{seed}:{field.name}").sample(nonempty, min(sample_size, len(nonempty)))
        matches = sum(bool(NRIC_PATTERN.fullmatch(value)) for value in sample)
        is_detected = matches >= threshold
        details[field.name] = {"sample_count": len(sample), "nric_match_count": matches, "nric_detected": is_detected}
        if is_detected:
            detected.append(field.name)
    return tuple(detected), details


def _java_hashcode(value: str) -> int:
    counter = 0
    for index, char in enumerate(reversed(value)):
        counter += ord(char) * pow(31, index)
    return counter % 2147483648


def _build_iv(key: str) -> bytes:
    return (
        hex(_java_hashcode(key[:8])).removeprefix("0x").zfill(8).upper()
        + hex(_java_hashcode(key[8:])).removeprefix("0x").zfill(8).upper()
    ).encode("utf-8")


def _extract_key(secret_value: str) -> str:
    try:
        parsed = json.loads(secret_value)
    except json.JSONDecodeError:
        return secret_value.strip()
    if not isinstance(parsed, dict):
        raise ValueError("Encryption secret must be a string or JSON object")
    for name in ("key", "encryption_key", "ENCRYPTION_KEY", "value"):
        if parsed.get(name):
            return str(parsed[name]).strip()
    raise ValueError("Encryption secret JSON must contain key, encryption_key, ENCRYPTION_KEY, or value")


def _material_from_secret_text(key: str) -> EncryptionMaterial:
    aes_key = key.encode("utf-8")[:32]
    if len(aes_key) != 32:
        raise ValueError("Encryption key must contain at least 32 UTF-8 bytes")
    return EncryptionMaterial(aes_key=aes_key, iv=_build_iv(key))


def _coerce_material(key: EncryptionMaterial | bytes) -> EncryptionMaterial:
    if isinstance(key, EncryptionMaterial):
        return key
    return _material_from_secret_text(key.decode("utf-8", errors="strict"))


@lru_cache(maxsize=1)
def encryption_key() -> EncryptionMaterial:
    """Fetch and cache the key without logging its content."""
    # Do not inherit an unrelated AWS_REGION from the local UI/Glue environment:
    # this named secret is deliberately located in ap-southeast-1.
    client = boto3.client("secretsmanager", region_name=os.environ.get("DATA_INSIGHT_ETL_ENCRYPTION_REGION", "ap-southeast-1"))
    response = client.get_secret_value(SecretId=os.environ.get("DATA_INSIGHT_ETL_ENCRYPTION_SECRET_ARN", SECRET_ARN))
    raw = response.get("SecretString")
    if raw is None:
        raw = base64.b64decode(response["SecretBinary"]).decode("utf-8")
    key = _extract_key(raw)
    return _material_from_secret_text(key)


def _normalise_value(value: Any) -> str | None:
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return None if not text or text.lower() in {"nan", "none", "nat"} else text


def _legacy_cbc_ciphertext(value: str, key: EncryptionMaterial) -> bool:
    try:
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) % AES.block_size:
            return False
        plain = unpad(AES.new(key.aes_key, AES.MODE_CBC, key.iv).decrypt(raw), AES.block_size)
        decoded = plain.decode("utf-8")
        return bool(decoded.strip()) and all(char.isprintable() for char in decoded)
    except (ValueError, UnicodeDecodeError):
        return False


def encrypted_identifier(value: Any, key: EncryptionMaterial | bytes) -> tuple[Any, bool]:
    material = _coerce_material(key)
    plain = _normalise_value(value)
    if plain is None:
        return pd.NA, False
    if plain.startswith(ENCRYPTED_PREFIX):
        return plain, True
    if _legacy_cbc_ciphertext(plain, material):
        # Legacy offline encryption used the same deterministic AES-CBC key
        # but did not label its ciphertext. Keep its bytes intact (never
        # decrypt/re-encrypt), while making all newly staged values use the
        # current representation.
        return ENCRYPTED_PREFIX + plain, True
    cipher = AES.new(material.aes_key, AES.MODE_CBC, material.iv)
    encrypted = base64.b64encode(cipher.encrypt(pad(plain.encode("utf-8"), AES.block_size))).decode("utf-8")
    return ENCRYPTED_PREFIX + encrypted, False


def age_band(value: Any) -> str | object:
    plain = _normalise_value(value)
    if plain is None:
        return pd.NA
    # The upstream Parquet may already have passed through the approved
    # five-year-banding process.  Preserve that representation so sanitising a
    # file for a second time cannot turn a safe age band into NULL.
    if re.fullmatch(r"(?:[0-8]?\d)-(?:[0-8]?\d)|90\+", plain):
        lower, _, upper = plain.partition("-")
        if upper and int(lower) % 5 == 0 and int(upper) == int(lower) + 4 and 0 <= int(lower) <= 85:
            return plain
        if plain == "90+":
            return plain
    try:
        years = int(float(plain))
    except ValueError:
        return pd.NA
    if years < 0:
        return pd.NA
    if years >= 90:
        return "90+"
    return f"{(years // 5) * 5}-{(years // 5) * 5 + 4}"


def postal_prefix(value: Any) -> str | object:
    plain = _normalise_value(value)
    if plain is None:
        return pd.NA
    digits = re.sub(r"\D", "", plain)
    if not digits:
        return pd.NA
    return digits[:2] + "0000" if len(digits) >= 2 else digits + "00000"


def sanitise_table(
    table: pa.Table, key: EncryptionMaterial | bytes | None = None,
    manual_encryption_columns: Iterable[str] = (), nric_columns: Iterable[str] = (),
) -> tuple[pa.Table, dict[str, Any]]:
    """Apply the approved union policy and return a value-free audit summary."""
    additional = set(manual_encryption_columns) | set(nric_columns)
    schema, plan = sanitised_schema(table.schema, additional)
    frame = table.to_pandas()
    frame = frame.drop(columns=list(plan.drop_columns), errors="ignore").copy()
    already_encrypted = 0
    encrypted_values = 0
    encrypted_columns = tuple(name for name in table.schema.names if name in (set(plan.identifier_columns) | additional) and name not in plan.drop_columns)
    if encrypted_columns:
        active_key = key or encryption_key()
    for column in encrypted_columns:
        normalised = frame[column].map(_normalise_value)
        unique_values = [value for value in normalised.dropna().unique().tolist()]
        mapping = {value: encrypted_identifier(value, active_key) for value in unique_values}
        output = normalised.map(mapping)
        # ``Series.map(mapping)`` leaves an absent source value as NaN rather
        # than calling ``encrypted_identifier``.  Only mapped values are the
        # `(ciphertext, already_encrypted)` tuples; preserve every other
        # value as a nullable string instead of subscripting a float NaN.
        frame[column] = output.map(
            lambda result: result[0] if isinstance(result, tuple) else pd.NA
        ).astype("string")
        already = int(sum(result[1] for result in mapping.values()))
        already_encrypted += already
        encrypted_values += len(mapping) - already
    for column in plan.postal_columns:
        frame[column] = frame[column].apply(postal_prefix).astype("string")
    for column in plan.age_columns:
        frame[column] = frame[column].apply(age_band).astype("string")
    result = pa.Table.from_pandas(frame, preserve_index=False).cast(schema, safe=False)
    return result, {
        "dropped_columns": list(plan.drop_columns),
        "encrypted_columns": list(encrypted_columns),
        "manual_encryption_columns": sorted(set(manual_encryption_columns)),
        "nric_encrypted_columns": sorted(set(nric_columns)),
        "postal_columns": list(plan.postal_columns),
        "age_banded_columns": list(plan.age_columns),
        "newly_encrypted_values": encrypted_values,
        "already_encrypted_values": already_encrypted,
    }
