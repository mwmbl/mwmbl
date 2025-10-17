import pytest
import json
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
def test_large_url_list_post_method(client, verified_user, access_token):
    """Test handling a large number of URLs (more than 160) that would fail with GET"""
    # Generate 200 URLs to test well beyond the previous threshold of ~160
    urls = []
    query = "test query"
    
    for i in range(200):
        # Generate URLs with varying lengths and some special characters
        url = f'https://example{i}.com/path/to/resource?param=value&id={i}&data={"some,data,with,commas"}'
        urls.append(url)
        
        # Create some votes for variety
        vote_type = 'upvote' if i % 3 == 0 else 'downvote'
        if i % 2 == 0:  # Create votes for every other URL
            SearchResultVote.objects.create(
                user=verified_user,
                url=url,
                query=query,
                vote_type=vote_type
            )
    
    # This would have failed with the old GET method due to URL length limits
    vote_stats_request = {
        "query": query,
        "urls": urls
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
    assert len(response_data['votes']) == 200
    
    # Verify all URLs are present and have correct vote counts
    votes_with_data = 0
    for i, url in enumerate(urls):
        assert url in response_data['votes']
        if i % 2 == 0:  # URLs that have votes
            votes_with_data += 1
            expected_vote_type = 'upvote' if i % 3 == 0 else 'downvote'
            assert response_data['votes'][url]['user_vote'] == expected_vote_type
            if expected_vote_type == 'upvote':
                assert response_data['votes'][url]['upvotes'] == 1
                assert response_data['votes'][url]['downvotes'] == 0
            else:
                assert response_data['votes'][url]['upvotes'] == 0
                assert response_data['votes'][url]['downvotes'] == 1
        else:  # URLs without votes
            assert response_data['votes'][url]['upvotes'] == 0
            assert response_data['votes'][url]['downvotes'] == 0
            assert response_data['votes'][url]['user_vote'] is None
    
    assert votes_with_data == 100  # Half of 200 URLs should have votes


@pytest.mark.django_db
def test_very_long_urls_in_large_list(client, verified_user, access_token):
    """Test handling URLs with very long paths that would be problematic in query strings"""
    base_urls = []
    query = "complex query with spaces and special chars !@#$%"
    
    # Create some extremely long URLs that would definitely exceed URL limits in GET
    for i in range(50):
        long_path = "/very/long/path/" + "/segment" * 20 + f"/file{i}"
        long_query_params = "&".join([f"param{j}=very_long_value_with_special_chars_{j}" for j in range(10)])
        url = f'https://verylongdomainname{i}.example.com{long_path}?{long_query_params}'
        base_urls.append(url)
        
        # Create votes for some URLs
        if i % 3 == 0:
            SearchResultVote.objects.create(
                user=verified_user,
                url=url,
                query=query,
                vote_type='upvote'
            )
    
    # Add the original problematic URLs from the bug report
    problematic_urls = [
        'http://keybase.io/hello',
        'http://keybase.io/hello_',
        'https://github.com/hello'
    ]
    base_urls.extend(problematic_urls)
    
    vote_stats_request = {
        "query": query,
        "urls": base_urls
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    assert len(response_data['votes']) == len(base_urls)
    
    # Verify the original problematic URLs are handled correctly
    for url in problematic_urls:
        assert url in response_data['votes']
        assert response_data['votes'][url]['upvotes'] == 0
        assert response_data['votes'][url]['downvotes'] == 0
        assert response_data['votes'][url]['user_vote'] is None


@pytest.mark.django_db  
def test_edge_case_empty_and_malformed_urls(client, verified_user, access_token):
    """Test edge cases with empty strings and malformed URLs in the list"""
    urls = [
        'https://valid-url.com/path',
        '',  # Empty string
        'https://another-valid.com/path',
        'not-a-valid-url',  # Malformed URL
        'https://third-valid.com/path',
    ]
    query = "edge case test"
    
    # Create vote for the first valid URL
    SearchResultVote.objects.create(
        user=verified_user,
        url='https://valid-url.com/path',
        query=query,
        vote_type='upvote'
    )
    
    vote_stats_request = {
        "query": query,
        "urls": urls
    }
    
    response = client.post(
        '/api/v1/platform/search-results/votes',
        data=json.dumps(vote_stats_request),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    
    assert response.status_code == 200
    
    response_data = response.json()
    # Should include all URLs (even empty and malformed ones)
    assert len(response_data['votes']) == len(urls)
    
    # Valid URL should have vote data
    assert response_data['votes']['https://valid-url.com/path']['user_vote'] == 'upvote'
    
    # Empty and malformed URLs should have no votes
    assert response_data['votes']['']['upvotes'] == 0
    assert response_data['votes']['not-a-valid-url']['upvotes'] == 0
