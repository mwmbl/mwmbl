import pytest
import json
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import SearchResultVote


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
def user2():
    """Create a second test user"""
    return User.objects.create_user(
        username='testuser2',
        email='test2@example.com',
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
def verified_user2(user2):
    """Create a second user with verified email"""
    EmailAddress.objects.create(
        user=user2,
        email='test2@example.com',
        verified=True,
        primary=True
    )
    return user2


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
def access_token2(verified_user2):
    """Generate JWT access token for second verified user"""
    refresh = RefreshToken.for_user(verified_user2)
    return str(refresh.access_token)


@pytest.fixture
def unverified_access_token(unverified_user):
    """Generate JWT access token for unverified user"""
    refresh = RefreshToken.for_user(unverified_user)
    return str(refresh.access_token)


@pytest.fixture
def sample_vote_data():
    """Sample vote data for testing"""
    return {
        "url": "https://example.com/test-page",
        "query": "test search query",
        "vote_type": "upvote"
    }


@pytest.mark.django_db
def test_vote_on_search_result_success(client, verified_user, access_token, sample_vote_data):
    """Test successfully voting on a search result"""
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(sample_vote_data),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert response_data['status'] == 'ok'
    assert 'Vote created successfully' in response_data['message']
    
    # Verify vote was created in database
    vote = SearchResultVote.objects.get(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query']
    )
    assert vote.vote_type == 'upvote'


@pytest.mark.django_db
def test_vote_update_existing_vote(client, verified_user, access_token, sample_vote_data):
    """Test updating an existing vote"""
    # Create initial vote
    SearchResultVote.objects.create(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query'],
        vote_type='upvote'
    )
    
    # Update to downvote
    updated_vote_data = sample_vote_data.copy()
    updated_vote_data['vote_type'] = 'downvote'
    
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(updated_vote_data),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert response_data['status'] == 'ok'
    assert 'Vote updated successfully' in response_data['message']
    
    # Verify vote was updated
    vote = SearchResultVote.objects.get(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query']
    )
    assert vote.vote_type == 'downvote'


@pytest.mark.django_db
def test_vote_invalid_vote_type(client, access_token, sample_vote_data):
    """Test voting with invalid vote type"""
    invalid_vote_data = sample_vote_data.copy()
    invalid_vote_data['vote_type'] = 'INVALID'
    
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(invalid_vote_data),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 422
    
    response_data = response.json()
    # Django Ninja validation errors have a different format
    assert 'detail' in response_data
    assert any('upvote' in str(error) and 'downvote' in str(error) for error in response_data['detail'])


@pytest.mark.django_db
def test_vote_without_authentication(client, sample_vote_data):
    """Test voting without JWT token"""
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(sample_vote_data),
        content_type='application/json'
    )
    
    assert response.status_code == 401


@pytest.mark.django_db
def test_vote_with_unverified_email(client, unverified_access_token, sample_vote_data):
    """Test voting with unverified email address"""
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(sample_vote_data),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {unverified_access_token}'
    )
    
    assert response.status_code == 403
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'Email address is not verified' in response_data['message']


@pytest.mark.django_db
def test_get_vote_counts_single_url(client, verified_user, verified_user2, access_token, sample_vote_data):
    """Test getting vote counts for a single URL"""
    # Create votes from different users
    SearchResultVote.objects.create(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query'],
        vote_type='upvote'
    )
    SearchResultVote.objects.create(
        user=verified_user2,
        url=sample_vote_data['url'],
        query=sample_vote_data['query'],
        vote_type='downvote'
    )
    
    response = client.get(
        f'/api/v1/platform/search-results/votes?query={sample_vote_data["query"]}&url={sample_vote_data["url"]}',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert 'votes' in response_data
    assert sample_vote_data['url'] in response_data['votes']
    
    vote_stats = response_data['votes'][sample_vote_data['url']]
    assert vote_stats['upvotes'] == 1
    assert vote_stats['downvotes'] == 1
    assert vote_stats['user_vote'] == 'upvote'  # Current user's vote


@pytest.mark.django_db
def test_get_vote_counts_multiple_urls(client, verified_user, access_token):
    """Test getting vote counts for multiple URLs"""
    url1 = "https://example.com/page1"
    url2 = "https://example.com/page2"
    query = "test query"
    
    # Create votes for different URLs
    SearchResultVote.objects.create(
        user=verified_user,
        url=url1,
        query=query,
        vote_type='upvote'
    )
    SearchResultVote.objects.create(
        user=verified_user,
        url=url2,
        query=query,
        vote_type='downvote'
    )
    
    response = client.get(
        f'/api/v1/platform/search-results/votes?query={query}&url={url1}&url={url2}',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert len(response_data['votes']) == 2
    assert url1 in response_data['votes']
    assert url2 in response_data['votes']
    
    assert response_data['votes'][url1]['user_vote'] == 'upvote'
    assert response_data['votes'][url2]['user_vote'] == 'downvote'


@pytest.mark.django_db
def test_get_vote_counts_no_urls(client, access_token):
    """Test getting vote counts without providing URLs"""
    response = client.get(
        '/api/v1/platform/search-results/votes?query=test',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 400
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'At least one URL must be provided' in response_data['message']


@pytest.mark.django_db
def test_remove_vote_success(client, verified_user, access_token, sample_vote_data):
    """Test successfully removing a vote"""
    # Create a vote first
    SearchResultVote.objects.create(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query'],
        vote_type='upvote'
    )
    
    response = client.delete(
        '/api/v1/platform/search-results/vote',
        data=json.dumps({
            'url': sample_vote_data['url'],
            'query': sample_vote_data['query']
        }),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert response_data['status'] == 'ok'
    assert 'Vote removed successfully' in response_data['message']
    
    # Verify vote was deleted
    assert not SearchResultVote.objects.filter(
        user=verified_user,
        url=sample_vote_data['url'],
        query=sample_vote_data['query']
    ).exists()


@pytest.mark.django_db
def test_remove_nonexistent_vote(client, access_token, sample_vote_data):
    """Test removing a vote that doesn't exist"""
    response = client.delete(
        '/api/v1/platform/search-results/vote',
        data=json.dumps({
            'url': sample_vote_data['url'],
            'query': sample_vote_data['query']
        }),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 404
    
    response_data = response.json()
    assert response_data['status'] == 'error'
    assert 'No vote found to remove' in response_data['message']


@pytest.mark.django_db
def test_get_user_vote_history(client, verified_user, access_token):
    """Test getting user's voting history"""
    # Create multiple votes
    votes_data = [
        {"url": "https://example.com/page1", "query": "query1", "vote_type": "upvote"},
        {"url": "https://example.com/page2", "query": "query2", "vote_type": "downvote"},
        {"url": "https://example.com/page3", "query": "query3", "vote_type": "upvote"},
    ]
    
    for vote_data in votes_data:
        SearchResultVote.objects.create(
            user=verified_user,
            **vote_data
        )
    
    response = client.get(
        '/api/v1/platform/search-results/my-votes',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert 'items' in response_data  # Paginated response
    assert len(response_data['items']) == 3
    
    # Verify the votes are ordered by timestamp (newest first)
    for i, vote in enumerate(response_data['items']):
        assert vote['url'] == votes_data[2-i]['url']  # Reverse order due to ordering by -timestamp
        assert vote['query'] == votes_data[2-i]['query']
        assert vote['vote_type'] == votes_data[2-i]['vote_type']


@pytest.mark.django_db
def test_vote_unique_constraint(verified_user):
    """Test that the unique constraint works (one vote per user per URL per query)"""
    from django.db import IntegrityError, transaction
    
    vote_data = {
        "user": verified_user,
        "url": "https://example.com/test",
        "query": "test query",
        "vote_type": "upvote"
    }
    
    # Create first vote
    vote1 = SearchResultVote.objects.create(**vote_data)
    
    # Try to create duplicate vote - should raise IntegrityError
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SearchResultVote.objects.create(**vote_data)
    
    # Verify only one vote exists (in a new transaction)
    with transaction.atomic():
        count = SearchResultVote.objects.filter(
            user=verified_user,
            url=vote_data['url'],
            query=vote_data['query']
        ).count()
        assert count == 1
