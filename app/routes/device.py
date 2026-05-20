from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter()


@router.get("/device/status", response_class=HTMLResponse)
def device_status(request: Request):
    return templates.TemplateResponse(
        request,
        "_fragments/device_status.html",
        {"device": request.app.state.device_state.detect},
    )
