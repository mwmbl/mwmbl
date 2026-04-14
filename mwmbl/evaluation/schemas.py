from ninja import Schema, Field
from typing import Optional
from datetime import datetime


class WasmSubmissionResponse(Schema):
    """Response returned after successfully submitting a WASM file for evaluation."""

    job_id: int = Field(
        description="Unique identifier for the created evaluation job.",
        example=42,
    )
    status: str = Field(
        description="Initial status of the job. Will be `VALIDATED` if the file passed validation.",
        example="VALIDATED",
    )
    message: str = Field(
        description="Human-readable message describing the outcome of the submission.",
        example="WASM file validated and job created successfully",
    )


class EvaluationJobResponse(Schema):
    """Summary of an evaluation job, as returned by the list-jobs endpoint."""

    id: int = Field(
        description="Unique identifier for the evaluation job.",
        example=42,
    )
    status: str = Field(
        description=(
            "Current status of the job. One of: `VALIDATED`, `RUNNING`, `COMPLETED`, `FAILED`."
        ),
        example="COMPLETED",
    )
    created_at: datetime = Field(
        description="Timestamp when the job was created.",
        example="2024-06-01T12:00:00Z",
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the job finished (null if still running or not yet started).",
        example="2024-06-01T12:05:00Z",
    )
    results: Optional[dict] = Field(
        default=None,
        description=(
            "Evaluation results as a JSON object. Present only when status is `COMPLETED`. "
            "Includes fields such as `ndcg_score` and `queries_evaluated`."
        ),
        example={"ndcg_score": 0.75, "queries_evaluated": 100},
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details if the job failed. Null otherwise.",
        example=None,
    )


class EvaluationResultsResponse(Schema):
    """Detailed results for a single evaluation job."""

    job_id: int = Field(
        description="Unique identifier for the evaluation job.",
        example=42,
    )
    status: str = Field(
        description=(
            "Current status of the job. One of: `VALIDATED`, `RUNNING`, `COMPLETED`, `FAILED`."
        ),
        example="COMPLETED",
    )
    results: Optional[dict] = Field(
        default=None,
        description=(
            "Evaluation results as a JSON object. Present only when status is `COMPLETED`. "
            "Includes fields such as `ndcg_score` and `queries_evaluated`."
        ),
        example={"ndcg_score": 0.75, "queries_evaluated": 100, "message": "Evaluation completed successfully"},
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details if the job failed. Null otherwise.",
        example=None,
    )
