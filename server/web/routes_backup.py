"""Backup/restore routes for the Settings panel: list, create-now, restore-by-name."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.core import backups

router = APIRouter()


class RestoreRequest(BaseModel):
    name: str


@router.get("/backups")
def get_backups() -> dict:
    return backups.list_backups()


@router.post("/backups")
def post_backup() -> dict:
    try:
        return backups.create_backup("manual")
    except backups.BackupBusyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/backups/restore")
def post_restore(body: RestoreRequest) -> dict:
    try:
        return backups.restore_backup(body.name)
    except backups.InvalidBackupNameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except backups.BackupNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except backups.BackupBusyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
