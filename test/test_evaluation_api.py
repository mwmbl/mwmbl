import pytest
import io
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import WasmEvaluationJob


User = get_user_model()


@pytest.fixture
def user():
    """Create a test user"""
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.fixture
def verified_user(user):
    """Create a user with verified email"""
    EmailAddress.objects.create(
        user=user,
        email='test@example.com',
        verified=True,
        primary=True
    )
    return user


@pytest.fixture
def unverified_user():
    """Create a user with unverified email"""
    user = User.objects.create_user(
        username='unverified',
        email='unverified@example.com',
        password='testpass123'
    )
    EmailAddress.objects.create(
        user=user,
        email='unverified@example.com',
        verified=False,
        primary=True
    )
    return user


@pytest.fixture
def access_token(verified_user):
    """Generate JWT access token for verified user"""
    refresh = RefreshToken.for_user(verified_user)
    return str(refresh.access_token)


@pytest.fixture
def unverified_access_token(unverified_user):
    """Generate JWT access token for unverified user"""
    refresh = RefreshToken.for_user(unverified_user)
    return str(refresh.access_token)


@pytest.fixture
def valid_wasm_bytes():
    """Create minimal valid WASM file bytes"""
    # This is a minimal WASM module with memory export
    # Generated using: wat2wasm with the following WAT:
    # (module
    #   (memory (export "memory") 1)
    # )
    return bytes([
        0x00, 0x61, 0x73, 0x6d,  # WASM magic number
        0x01, 0x00, 0x00, 0x00,  # WASM version
        0x05, 0x03, 0x01, 0x00, 0x01,  # Memory section: 1 page minimum
        0x07, 0x0a, 0x01, 0x06, 0x6d, 0x65, 0x6d, 0x6f, 0x72, 0x79, 0x02, 0x00  # Export section: export "memory"
    ])


@pytest.fixture
def invalid_wasm_bytes():
    """Create invalid WASM file bytes"""
    return b'invalid wasm content'


@pytest.mark.django_db
def test_submit_valid_wasm_file(client, verified_user, access_token, valid_wasm_bytes):
    """Test submitting a valid WASM file"""
    wasm_file = io.BytesIO(valid_wasm_bytes)
    wasm_file.name = 'test_ranker.wasm'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': wasm_file
        },
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert 'job_id' in response_data
    assert response_data['status'] == 'VALIDATED'
    assert 'message' in response_data
    
    # Verify job was created in database
    job = WasmEvaluationJob.objects.get(id=response_data['job_id'])
    assert job.user == verified_user
    assert job.status == 'VALIDATED'
    assert job.wasm_file == valid_wasm_bytes


@pytest.mark.django_db
def test_submit_invalid_wasm_file(client, access_token, invalid_wasm_bytes):
    """Test submitting an invalid WASM file"""
    wasm_file = io.BytesIO(invalid_wasm_bytes)
    wasm_file.name = 'invalid.wasm'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': wasm_file
        },
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 400
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'WASM validation failed' in response_data['message']


@pytest.mark.django_db
def test_submit_non_wasm_file(client, access_token):
    """Test submitting a file without .wasm extension"""
    text_file = io.BytesIO(b'some text content')
    text_file.name = 'test.txt'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': text_file
        },
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 400
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'File must have .wasm extension' in response_data['message']


@pytest.mark.django_db
def test_submit_large_file(client, access_token):
    """Test submitting a file that exceeds size limit"""
    # Create a file larger than 10MB
    large_content = b'x' * (11 * 1024 * 1024)  # 11MB
    large_file = io.BytesIO(large_content)
    large_file.name = 'large.wasm'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': large_file
        },
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 400
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'File too large' in response_data['message']


@pytest.mark.django_db
def test_submit_without_authentication(client, valid_wasm_bytes):
    """Test submitting without JWT token"""
    wasm_file = io.BytesIO(valid_wasm_bytes)
    wasm_file.name = 'test.wasm'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': wasm_file
        }
    )
    
    assert response.status_code == 401


@pytest.mark.django_db
def test_submit_with_unverified_email(client, unverified_access_token, valid_wasm_bytes):
    """Test submitting with unverified email address"""
    wasm_file = io.BytesIO(valid_wasm_bytes)
    wasm_file.name = 'test.wasm'
    
    response = client.post(
        '/api/v1/evaluate/submit',
        {
            'file': wasm_file
        },
        HTTP_AUTHORIZATION=f'Bearer {unverified_access_token}'
    )
    
    assert response.status_code == 403
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'Email address is not verified' in response_data['message']


@pytest.mark.django_db
def test_submit_without_file(client, access_token):
    """Test submitting without a file"""
    response = client.post(
        '/api/v1/evaluate/submit',
        {},
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 422  # Validation error


@pytest.mark.django_db
def test_multiple_submissions_same_user(client, verified_user, access_token, valid_wasm_bytes):
    """Test that a user can submit multiple WASM files"""
    # Submit first file
    wasm_file1 = io.BytesIO(valid_wasm_bytes)
    wasm_file1.name = 'ranker1.wasm'
    
    response1 = client.post(
        '/api/v1/evaluate/submit',
        {'file': wasm_file1},
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response1.status_code == 200
    job_id1 = response1.json()['job_id']
    
    # Submit second file
    wasm_file2 = io.BytesIO(valid_wasm_bytes)
    wasm_file2.name = 'ranker2.wasm'
    
    response2 = client.post(
        '/api/v1/evaluate/submit',
        {'file': wasm_file2},
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response2.status_code == 200
    job_id2 = response2.json()['job_id']
    
    # Verify both jobs exist and belong to the same user
    assert job_id1 != job_id2
    
    job1 = WasmEvaluationJob.objects.get(id=job_id1)
    job2 = WasmEvaluationJob.objects.get(id=job_id2)
    
    assert job1.user == verified_user
    assert job2.user == verified_user
    assert job1.status == 'VALIDATED'
    assert job2.status == 'VALIDATED'
