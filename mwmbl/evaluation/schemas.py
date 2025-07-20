from ninja import Schema
from typing import Optional
from datetime import datetime


class WasmSubmissionResponse(Schema):
    job_id: int
    status: str
    message: str


class EvaluationJobResponse(Schema):
    id: int
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    results: Optional[dict] = None
    error_message: Optional[str] = None


class EvaluationResultsResponse(Schema):
    job_id: int
    status: str
    results: Optional[dict] = None
    error_message: Optional[str] = None
