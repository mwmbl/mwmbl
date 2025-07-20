from ninja import NinjaAPI, File, UploadedFile
from ninja_jwt.authentication import JWTAuth
from django.utils import timezone
from typing import List

from mwmbl.models import WasmEvaluationJob
from .schemas import WasmSubmissionResponse, EvaluationJobResponse, EvaluationResultsResponse
from .wasm_validator import WasmValidator


api = NinjaAPI(urls_namespace="evaluate")


class InvalidRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


@api.exception_handler(InvalidRequest)
def invalid_request_handler(request, exc: InvalidRequest):
    return api.create_response(
        request,
        {"status": "error", "message": exc.message},
        status=exc.status,
    )


def check_email_verified(request):
    """Check if user's email is verified"""
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address or not from_email_address.verified:
        raise InvalidRequest("Email address is not verified", status=403)


@api.post('/submit', response=WasmSubmissionResponse, auth=JWTAuth())
def submit_wasm(request, file: UploadedFile = File(...)):
    """Submit a WASM file for evaluation"""
    check_email_verified(request)
    
    # Basic file validation
    if file.size > 10 * 1024 * 1024:  # 10MB limit
        raise InvalidRequest("File too large. Maximum size is 10MB.")
    
    if not file.name.endswith('.wasm'):
        raise InvalidRequest("File must have .wasm extension.")
    
    # Read WASM bytes
    wasm_bytes = file.read()
    
    # Validate WASM file
    validation_result = WasmValidator.validate_wasm_file(wasm_bytes)
    
    if not validation_result['valid']:
        raise InvalidRequest(f"WASM validation failed: {validation_result['error']}")
    
    # Create evaluation job
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


@api.post('/run/{job_id}', response=EvaluationResultsResponse, auth=JWTAuth())
def run_evaluation(request, job_id: int):
    """Run evaluation for a submitted WASM file"""
    check_email_verified(request)
    
    try:
        job = WasmEvaluationJob.objects.get(id=job_id, user=request.user)
    except WasmEvaluationJob.DoesNotExist:
        raise InvalidRequest("Evaluation job not found", status=404)
    
    if job.status not in ['VALIDATED', 'FAILED']:
        raise InvalidRequest(f"Job cannot be run. Current status: {job.status}")
    
    # Update job status
    job.status = 'RUNNING'
    job.save()
    
    try:
        # For now, just return a mock result
        # In a full implementation, this would run the actual evaluation
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


@api.get('/results/{job_id}', response=EvaluationResultsResponse, auth=JWTAuth())
def get_evaluation_results(request, job_id: int):
    """Get evaluation results for a job"""
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


@api.get('/jobs', response=List[EvaluationJobResponse], auth=JWTAuth())
def list_evaluation_jobs(request):
    """List all evaluation jobs for the current user"""
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
