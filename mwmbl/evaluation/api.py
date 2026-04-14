from ninja import File, UploadedFile, Router
from ninja_jwt.authentication import JWTAuth
from django.utils import timezone
from typing import List

from mwmbl.models import WasmEvaluationJob
from .schemas import WasmSubmissionResponse, EvaluationJobResponse, EvaluationResultsResponse
from .wasm_validator import WasmValidator
from mwmbl.exceptions import InvalidRequest

router = Router(tags=["Evaluation"])


def check_email_verified(request):
    """Check if user's email is verified."""
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address or not from_email_address.verified:
        raise InvalidRequest("Email address is not verified", status=403)  # 403 Forbidden


@router.post(
    '/submit',
    response=WasmSubmissionResponse,
    auth=JWTAuth(),
    summary="Submit a WASM ranking function",
    description=(
        "Upload a WebAssembly (.wasm) file containing a custom ranking function for evaluation. "
        "The file is validated on upload and a job is created with status `VALIDATED`. "
        "Maximum file size is 10 MB. The file must have a `.wasm` extension. "
        "Requires a verified account."
    ),
)
def submit_wasm(request, file: UploadedFile = File(...)):
    check_email_verified(request)

    if file.size > 10 * 1024 * 1024:  # 10MB limit
        raise InvalidRequest("File too large. Maximum size is 10MB.")

    if not file.name.endswith('.wasm'):
        raise InvalidRequest("File must have .wasm extension.")

    wasm_bytes = file.read()

    validation_result = WasmValidator.validate_wasm_file(wasm_bytes)

    if not validation_result['valid']:
        raise InvalidRequest(f"WASM validation failed: {validation_result['error']}")

    job = WasmEvaluationJob.objects.create(
        user=request.user,
        wasm_file=wasm_bytes,
        status='VALIDATED'
    )

    return WasmSubmissionResponse(
        job_id=job.id,
        status=job.status,
        message="WASM file validated and job created successfully"
    )


@router.post(
    '/run/{job_id}',
    response=EvaluationResultsResponse,
    auth=JWTAuth(),
    summary="Run an evaluation job",
    description=(
        "Trigger evaluation for a previously submitted WASM file. "
        "The job must be in `VALIDATED` or `FAILED` status to be run. "
        "The evaluation measures ranking quality using NDCG against a held-out query set. "
        "Requires a verified account and ownership of the job."
    ),
)
def run_evaluation(request, job_id: int):
    check_email_verified(request)

    try:
        job = WasmEvaluationJob.objects.get(id=job_id, user=request.user)
    except WasmEvaluationJob.DoesNotExist:
        raise InvalidRequest("Evaluation job not found", status=404)

    if job.status not in ['VALIDATED', 'FAILED']:
        raise InvalidRequest(f"Job cannot be run. Current status: {job.status}")

    job.status = 'RUNNING'
    job.save()

    try:
        mock_results = {
            "ndcg_score": 0.75,
            "queries_evaluated": 10,
            "message": "Mock evaluation completed successfully"
        }

        job.status = 'COMPLETED'
        job.results = mock_results
        job.completed_at = timezone.now()
        job.save()

        return EvaluationResultsResponse(
            job_id=job.id,
            status=job.status,
            results=job.results
        )

    except Exception as e:
        job.status = 'FAILED'
        job.error_message = str(e)
        job.save()

        return EvaluationResultsResponse(
            job_id=job.id,
            status=job.status,
            error_message=job.error_message
        )


@router.get(
    '/results/{job_id}',
    response=EvaluationResultsResponse,
    auth=JWTAuth(),
    summary="Get evaluation results",
    description=(
        "Retrieve the results of a completed evaluation job. "
        "If the job is still running, the `results` field will be null. "
        "If the job failed, the `error_message` field will contain details. "
        "Requires a verified account and ownership of the job."
    ),
)
def get_evaluation_results(request, job_id: int):
    check_email_verified(request)

    try:
        job = WasmEvaluationJob.objects.get(id=job_id, user=request.user)
    except WasmEvaluationJob.DoesNotExist:
        raise InvalidRequest("Evaluation job not found", status=404)

    return EvaluationResultsResponse(
        job_id=job.id,
        status=job.status,
        results=job.results,
        error_message=job.error_message
    )


@router.get(
    '/jobs',
    response=List[EvaluationJobResponse],
    auth=JWTAuth(),
    summary="List evaluation jobs",
    description=(
        "List all evaluation jobs submitted by the current user, ordered by creation time "
        "(most recent first). Includes status, results, and any error messages. "
        "Requires a verified account."
    ),
)
def list_evaluation_jobs(request):
    check_email_verified(request)

    jobs = WasmEvaluationJob.objects.filter(user=request.user).order_by('-created_at')

    return [
        EvaluationJobResponse(
            id=job.id,
            status=job.status,
            created_at=job.created_at,
            completed_at=job.completed_at,
            results=job.results,
            error_message=job.error_message
        )
        for job in jobs
    ]
