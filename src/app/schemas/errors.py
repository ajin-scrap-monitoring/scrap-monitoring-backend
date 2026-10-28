"""Descriptions of existing error bodies; these models do not rewrite responses."""

from typing import Any

from pydantic import BaseModel


class HTTPErrorResponse(BaseModel):
    detail: str


class RequestValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str
    input: Any = None
    ctx: dict[str, Any] | None = None


class RequestValidationErrorResponse(BaseModel):
    detail: list[RequestValidationIssue]


class MediaErrorDetail(BaseModel):
    code: str
    message: str


class MediaErrorResponse(BaseModel):
    error: MediaErrorDetail
