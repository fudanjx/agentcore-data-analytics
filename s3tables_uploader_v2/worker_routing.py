"""Deterministic selection-time routing for leased uploader workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Iterable


MIB = 1024 * 1024
FORMAT_ALLOWANCES = {
    ".parquet.gzip": 128 * MIB,
    ".parquet": 128 * MIB,
    ".csv": 64 * MIB,
    ".tsv": 64 * MIB,
    ".xlsx": 32 * MIB,
    ".xls": 32 * MIB,
}


class RoutingError(ValueError):
    """Raised when a selected file cannot be routed safely."""


@dataclass(frozen=True)
class SelectedFile:
    name: str
    size_bytes: int


@dataclass(frozen=True)
class WorkerRoute:
    worker_size: str
    routing_score: float
    routing_reason: str


def _suffix(name: str) -> str:
    lower = PurePath(name).name.lower()
    for suffix in FORMAT_ALLOWANCES:
        if lower.endswith(suffix):
            return suffix
    raise RoutingError(f"Unsupported upload format: {name}")


def route_files(files: Iterable[SelectedFile]) -> WorkerRoute:
    items = list(files)
    if not items:
        raise RoutingError("Select at least one supported file")
    score = 0.0
    formats: list[str] = []
    for item in items:
        if not item.name or item.size_bytes <= 0:
            raise RoutingError("Each selected file must have a positive byte size")
        suffix = _suffix(item.name)
        score += item.size_bytes / FORMAT_ALLOWANCES[suffix]
        formats.append(suffix)
    worker_size = "BASE" if score <= 1.0 else "LARGE"
    reason = f"{worker_size}_{'_'.join(sorted(set(formats))).replace('.', '').upper()}_SCORE_{score:.3f}"
    return WorkerRoute(worker_size=worker_size, routing_score=score, routing_reason=reason)
