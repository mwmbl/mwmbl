import pytest
import json
from django.test import Client
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from ninja_jwt.tokens import RefreshToken

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
def access_token(verified_user):
    """Generate JWT access token for verified user"""
    refresh = RefreshToken.for_user(verified_user)
    return str(refresh.access_token)


@pytest.mark.django_db
def test_openapi_spec_generation():
    """Test that the OpenAPI spec can be generated without errors"""
    client = Client()
    
    # Test that the OpenAPI JSON endpoint works
    response = client.get('/api/v1/platform/openapi.json')
    assert response.status_code == 200
    
    # Parse the JSON to ensure it's valid
    openapi_spec = response.json()
    
    # Basic OpenAPI spec validation
    assert 'openapi' in openapi_spec
    assert 'info' in openapi_spec
    assert 'paths' in openapi_spec
    assert 'components' in openapi_spec
    
    # Check that our voting endpoints are present
    paths = openapi_spec['paths']
    assert '/api/v1/platform/search-results/vote' in paths
    assert '/api/v1/platform/search-results/votes' in paths
    assert '/api/v1/platform/search-results/my-votes' in paths
    
    # Check that POST endpoint for voting exists
    vote_endpoint = paths['/api/v1/platform/search-results/vote']
    assert 'post' in vote_endpoint
    assert 'delete' in vote_endpoint
    
    # Check that GET endpoint for vote counts exists
    votes_endpoint = paths['/api/v1/platform/search-results/votes']
    assert 'get' in votes_endpoint
    
    # Check that GET endpoint for user vote history exists
    my_votes_endpoint = paths['/api/v1/platform/search-results/my-votes']
    assert 'get' in my_votes_endpoint


@pytest.mark.django_db
def test_openapi_docs_page():
    """Test that the OpenAPI docs page loads without errors"""
    client = Client()
    
    # Test that the docs page works
    response = client.get('/api/v1/platform/docs')
    assert response.status_code == 200
    assert 'text/html' in response['Content-Type']


@pytest.mark.django_db
def test_voting_schemas_in_openapi():
    """Test that our voting schemas are properly defined in the OpenAPI spec"""
    client = Client()
    
    response = client.get('/api/v1/platform/openapi.json')
    assert response.status_code == 200
    
    openapi_spec = response.json()
    components = openapi_spec.get('components', {})
    schemas = components.get('schemas', {})
    
    # Check that our voting schemas are present
    assert 'VoteRequest' in schemas
    assert 'VoteStats' in schemas
    assert 'VoteResponse' in schemas
    assert 'UserVoteHistory' in schemas
    assert 'VoteRemoveRequest' in schemas
    
    # Check VoteRequest schema structure
    vote_request_schema = schemas['VoteRequest']
    assert 'properties' in vote_request_schema
    properties = vote_request_schema['properties']
    
    assert 'url' in properties
    assert 'query' in properties
    assert 'vote_type' in properties
    
    # Check that vote_type has the correct enum values
    vote_type_property = properties['vote_type']
    assert 'enum' in vote_type_property
    assert 'upvote' in vote_type_property['enum']
    assert 'downvote' in vote_type_property['enum']
    
    # Check that descriptions are present
    assert 'description' in vote_type_property
    assert 'description' in properties['url']
    assert 'description' in properties['query']


@pytest.mark.django_db
def test_voting_endpoint_documentation():
    """Test that voting endpoints have proper documentation in OpenAPI spec"""
    client = Client()
    
    response = client.get('/api/v1/platform/openapi.json')
    assert response.status_code == 200
    
    openapi_spec = response.json()
    paths = openapi_spec['paths']
    
    # Check POST /search-results/vote endpoint documentation
    vote_post = paths['/api/v1/platform/search-results/vote']['post']
    assert 'summary' in vote_post
    assert 'description' in vote_post
    assert 'tags' in vote_post
    assert 'Search Result Voting' in vote_post['tags']
    
    # Check GET /search-results/votes endpoint documentation
    votes_get = paths['/api/v1/platform/search-results/votes']['get']
    assert 'summary' in votes_get
    assert 'description' in votes_get
    assert 'tags' in votes_get
    assert 'Search Result Voting' in votes_get['tags']
    
    # Check DELETE /search-results/vote endpoint documentation
    vote_delete = paths['/api/v1/platform/search-results/vote']['delete']
    assert 'summary' in vote_delete
    assert 'description' in vote_delete
    assert 'tags' in vote_delete
    assert 'Search Result Voting' in vote_delete['tags']
    
    # Check GET /search-results/my-votes endpoint documentation
    my_votes_get = paths['/api/v1/platform/search-results/my-votes']['get']
    assert 'summary' in my_votes_get
    assert 'description' in my_votes_get
    assert 'tags' in my_votes_get
    assert 'Search Result Voting' in my_votes_get['tags']


@pytest.mark.django_db
def test_schema_examples_in_openapi():
    """Test that our schemas have proper examples in the OpenAPI spec"""
    client = Client()
    
    response = client.get('/api/v1/platform/openapi.json')
    assert response.status_code == 200
    
    openapi_spec = response.json()
    schemas = openapi_spec['components']['schemas']
    
    # Check VoteRequest schema has examples
    vote_request = schemas['VoteRequest']
    properties = vote_request['properties']
    
    # Check that examples are present
    assert 'example' in properties['url']
    assert 'example' in properties['query']
    assert 'example' in properties['vote_type']
    
    # Check example values are reasonable
    assert 'https://' in properties['url']['example']
    assert properties['vote_type']['example'] in ['upvote', 'downvote']
    
    # Check VoteStats schema has examples
    vote_stats = schemas['VoteStats']
    vote_stats_properties = vote_stats['properties']
    
    assert 'example' in vote_stats_properties['upvotes']
    assert 'example' in vote_stats_properties['downvotes']
    assert isinstance(vote_stats_properties['upvotes']['example'], int)
    assert isinstance(vote_stats_properties['downvotes']['example'], int)
