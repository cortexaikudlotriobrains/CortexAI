"""Structured public Work errors."""

from __future__ import annotations

from fastapi import HTTPException


def work_http_error(status_code: int, code: str, message: str, **context: object) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    if context:
        detail["context"] = context
    return HTTPException(status_code=status_code, detail=detail)
