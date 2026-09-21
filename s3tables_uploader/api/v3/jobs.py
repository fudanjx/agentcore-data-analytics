"""Direct job and Glue ingestion status endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from ...app.dependencies import (
    GlueDep,
    SettingsDep,
    StoreDep,
    UserDep,
    enforce_ownership,
)
from ...job_store import MissingRecord
from ...utils.api_prefix import get_api_prefix


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


@router.get("/{job_id}")
def get_job(
    job_id: str,
    user: UserDep,
    store: StoreDep,
) -> dict[str, object]:
    try:
        request = store.get_request(job_id)
        status = store.get_status(job_id).status
    except MissingRecord as error:
        raise HTTPException(404, "JOB_NOT_FOUND") from error
    enforce_ownership(request.owner_user_id, user)
    return {"request": request.model_dump(mode="json"), "status": status.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Ingestion (Glue run status) — promoted from unversioned /api/ingestions
# ---------------------------------------------------------------------------

ingestions_router = APIRouter(prefix="/api/v3/ingestions")


@ingestions_router.get("/{job_run_id}")
def ingestion_status(
    job_run_id: str,
    _user: UserDep,
    glue: GlueDep,
    settings: SettingsDep,
) -> dict[str, object]:
    try:
        run = glue.get_job_run(
            JobName=settings.glue_job_name, RunId=job_run_id, PredecessorsIncluded=False
        )["JobRun"]
    except Exception as error:
        raise HTTPException(503, "Glue status is temporarily unavailable") from error
    state = str(run.get("JobRunState", "UNKNOWN"))
    message = str(
        run.get("ErrorMessage") or run.get("StateDetail") or f"Glue job is {state.lower()}."
    )
    return {"job_run_id": job_run_id, "state": state, "message": message}
