import pytest
import json
from urllib.parse import quote
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import SearchResultVote


User = get_user_model()


@pytest.fixture
def verified_user():
    """Create a user with verified email"""
    user = User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )
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
def test_urls_with_commas_single_url(client, verified_user, access_token):
    """Test handling URLs with commas using single URL parameter"""
    # URL with comma in the path (like the original issue)
    url_with_comma = 'https://en.wikipedia.org/wiki/"Hello,_World!"_program'
    query = "hello world"
    
    # Create a vote for this URL
    SearchResultVote.objects.create(
        user=verified_user,
        url=url_with_comma,
        query=query,
        vote_type='upvote'
    )
    
    # Test getting vote counts using POST
    vote_stats_request = {
        "query": query,
        "urls": [url_with_comma]
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert 'votes' in response_data
    assert url_with_comma in response_data['votes']
    
    vote_stats = response_data['votes'][url_with_comma]
    assert vote_stats['upvotes'] == 1
    assert vote_stats['downvotes'] == 0
    assert vote_stats['user_vote'] == 'upvote'


@pytest.mark.django_db
def test_urls_with_commas_multiple_urls(client, verified_user, access_token):
    """Test handling multiple URLs with commas using POST"""
    # Multiple URLs with commas (from the original issue)
    url1 = 'https://en.wikipedia.org/wiki/"Hello,_World!"_program'
    url2 = 'https://en.wikipedia.org/wiki/Hello_Twelve,_Hello_Thirteen,_Hello_Love'
    query = "hello"
    
    # Create votes for these URLs
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
    
    # Test getting vote counts using POST
    vote_stats_request = {
        "query": query,
        "urls": [url1, url2]
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert len(response_data['votes']) == 2
    assert url1 in response_data['votes']
    assert url2 in response_data['votes']
    
    # Verify vote counts are correct
    assert response_data['votes'][url1]['upvotes'] == 1
    assert response_data['votes'][url1]['downvotes'] == 0
    assert response_data['votes'][url1]['user_vote'] == 'upvote'
    
    assert response_data['votes'][url2]['upvotes'] == 0
    assert response_data['votes'][url2]['downvotes'] == 1
    assert response_data['votes'][url2]['user_vote'] == 'downvote'


@pytest.mark.django_db
def test_urls_with_various_special_characters(client, verified_user, access_token):
    """Test URLs with various special characters that could cause parsing issues"""
    special_urls = [
        'https://example.com/path?param=value,with,commas',
        'https://example.com/path?param=value%26other=a,b,c',  # URL-encoded ampersand
        'https://example.com/path/with,comma/in/path',
        'https://example.com/path?query=hello%2Cworld',  # URL-encoded comma
        'https://example.com/path?data={"key":"value,with,comma"}',
    ]
    query = "special characters test"
    
    # Create votes for all URLs
    for i, url in enumerate(special_urls):
        vote_type = 'upvote' if i % 2 == 0 else 'downvote'
        SearchResultVote.objects.create(
            user=verified_user,
            url=url,
            query=query,
            vote_type=vote_type
        )
    
    # Test getting vote counts using POST
    vote_stats_request = {
        "query": query,
        "urls": special_urls
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert len(response_data['votes']) == len(special_urls)
    
    # Verify all URLs are present and have correct vote counts
    for i, url in enumerate(special_urls):
        assert url in response_data['votes']
        expected_vote_type = 'upvote' if i % 2 == 0 else 'downvote'
        assert response_data['votes'][url]['user_vote'] == expected_vote_type


@pytest.mark.django_db
def test_voting_on_url_with_commas(client, verified_user, access_token):
    """Test voting on URLs with commas works correctly"""
    url_with_comma = 'https://en.wikipedia.org/wiki/"Hello,_World!"_program'
    query = "hello world"
    
    vote_data = {
        "url": url_with_comma,
        "query": query,
        "vote_type": "upvote"
    }
    
    # Test voting
    response = client.post(
        '/api/v1/platform/search-results/vote',
        data=json.dumps(vote_data),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert response_data['status'] == 'ok'
    assert 'Vote created successfully' in response_data['message']
    
    # Verify vote was created in database with exact URL
    vote = SearchResultVote.objects.get(
        user=verified_user,
        url=url_with_comma,
        query=query
    )
    assert vote.vote_type == 'upvote'
    
    # Test retrieving the vote using POST
    vote_stats_request = {
        "query": query,
        "urls": [url_with_comma]
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    response_data = response.json()
    assert url_with_comma in response_data['votes']
    assert response_data['votes'][url_with_comma]['user_vote'] == 'upvote'
