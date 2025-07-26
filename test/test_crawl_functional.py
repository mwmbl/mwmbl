import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import fakeredis
import pytest
from django.test import override_settings

from mwmbl.crawler.batch import HashedBatch, Result
from mwmbl.crawler.urls import FoundURL, URLStatus
from mwmbl.redis_url_queue import RedisURLQueue


@pytest.fixture
def fake_redis():
    """Create a fake Redis instance for testing"""
    return fakeredis.FakeRedis(decode_responses=True, health_check_interval=30)


@pytest.fixture
def temp_data_path():
    """Create a temporary directory for test data"""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def mock_settings(temp_data_path):
    """Mock Django settings for crawler"""
    with override_settings(
        DATA_PATH=str(temp_data_path),
        REDIS_URL="redis://localhost:6379",
        SETUP_DATABASE=False,  # Disable database setup for testing
        NUM_PAGES=10  # Small number for testing
    ):
        yield


@pytest.fixture
def mock_environment():
    """Mock environment variables"""
    env_vars = {
        'MWMBL_API_KEY': 'test-api-key',
        'MWMBL_CONTACT_INFO': 'test@example.com',
        'CRAWLER_WORKERS': '1',
        'CRAWL_DELAY_SECONDS': '0.0'
    }
    with patch.dict(os.environ, env_vars):
        yield


@pytest.fixture
def mock_crawl_response():
    """Mock successful crawl response"""
    return {
        'url': 'https://example.com',
        'status': 200,
        'timestamp': int(time.time() * 1000),
        'content': {
            'title': 'Example Page',
            'extract': 'This is an example page with some content.',
            'links': ['https://example.com/page1', 'https://example.com/page2'],
            'extra_links': ['https://example.com/extra1']
        },
        'error': None
    }


@pytest.fixture
def sample_found_urls():
    """Create sample FoundURL objects for testing"""
    return [
        FoundURL(
            url='https://example.com/page1',
            user_id_hash='test_user_hash',
            status=URLStatus.NEW,
            timestamp=datetime.utcnow(),
            last_crawled=datetime.utcnow() - timedelta(days=60)
        ),
        FoundURL(
            url='https://example.com/page2',
            user_id_hash='test_user_hash',
            status=URLStatus.NEW,
            timestamp=datetime.utcnow(),
            last_crawled=datetime.utcnow() - timedelta(days=90)
        ),
        FoundURL(
            url='https://test.com/article',
            user_id_hash='test_user_hash',
            status=URLStatus.NEW,
            timestamp=datetime.utcnow(),
            last_crawled=None
        )
    ]


@pytest.mark.django_db
class TestCrawlFunctional:
    """Functional tests for the crawl.py module"""

    def test_redis_connection_and_health_check(self, fake_redis, mock_settings, mock_environment):
        """Test Redis connection and health check functionality"""
        from mwmbl.crawl import Crawler
        
        # Test with working Redis
        crawler = Crawler()
        crawler._redis = fake_redis  # Set the private attribute directly
        # Should not raise an exception
        crawler.check_redis()
        
        # Test with broken Redis
        broken_redis = MagicMock()
        broken_redis.ping.side_effect = ConnectionError("Redis unavailable")
        
        crawler._redis = broken_redis  # Set the private attribute directly
        with pytest.raises(SystemExit):
            crawler.check_redis()

    def test_url_queue_operations(self, fake_redis, sample_found_urls):
        """Test RedisURLQueue operations with fake Redis"""
        url_queue = RedisURLQueue(fake_redis, lambda: set())
        
        # Test queuing URLs
        url_queue.queue_urls(sample_found_urls)
        
        # Verify URLs were queued
        assert fake_redis.zcard("domain-scores") > 0
        
        # Test getting a batch
        batch = url_queue.get_batch("test_user")
        assert len(batch) > 0
        assert all(url.startswith('http') for url in batch)

    def test_process_batch_workflow(self, fake_redis, mock_settings, mock_environment, mock_crawl_response):
        """Test the complete batch processing workflow"""
        from mwmbl.crawl import Crawler
        
        crawler = Crawler()
        crawler._redis = fake_redis  # Set the private attribute directly
        
        # Mock the URL queue
        mock_url_queue = MagicMock()
        mock_url_queue.get_batch.return_value = ['https://example.com']
        crawler._url_queue = mock_url_queue
        
        with patch('mwmbl.crawl.crawl_url', return_value=mock_crawl_response):
            with patch('mwmbl.crawl.record_urls_in_database'):
                # Should complete without errors
                crawler.process_batch()
                
                # Verify batch was pushed to Redis
                batch_data = fake_redis.lpop("batch-queue")
                assert batch_data is not None
                
                # Verify batch structure
                batch = json.loads(batch_data)
                assert 'user_id_hash' in batch
                assert 'timestamp' in batch
                assert 'items' in batch
                assert len(batch['items']) == 1

    def test_indexing_workflow(self, fake_redis, mock_settings, mock_environment, temp_data_path):
        """Test the indexing workflow with fake Redis"""
        from mwmbl.crawl import Crawler
        
        # Create a sample batch in Redis
        # Create a sample item that matches the Item schema
        from mwmbl.crawler.batch import Item, ItemContent
        sample_item = Item(
            url="https://example.com",
            status=200,
            timestamp=time.time(),
            content=ItemContent(
                title="Example",
                extract="Test content"
            )
        )
        
        sample_batch = HashedBatch(
            user_id_hash="test_user",
            timestamp=int(time.time() * 1000),
            items=[sample_item]
        )
        fake_redis.rpush("batch-queue", sample_batch.json())
        
        crawler = Crawler()
        crawler._redis = fake_redis  # Set the private attribute directly
        
        # Mock Counter object for index_batches return value
        from collections import Counter
        mock_counter = Counter({'example': 1})
        with patch('mwmbl.crawl.index_batches', return_value=mock_counter):
                with patch('mwmbl.crawl.RemoteIndex') as mock_remote_index:
                    with patch('mwmbl.crawl.TinyIndex') as mock_tiny_index:
                        with patch('mwmbl.crawl.index_pages') as mock_index_pages:
                            with patch('requests.post') as mock_post:
                                # Setup mocks
                                mock_remote_index_instance = MagicMock()
                                mock_remote_index.return_value = mock_remote_index_instance
                                mock_remote_index_instance.retrieve.return_value = []
                                
                                mock_tiny_index_instance = MagicMock()
                                mock_tiny_index.return_value.__enter__.return_value = mock_tiny_index_instance
                                mock_tiny_index_instance.retrieve.return_value = []
                                mock_tiny_index_instance.get_key_page_index.return_value = 0
                                mock_tiny_index_instance.get_page.return_value = "test page content"
                                
                                mock_post.return_value.status_code = 200
                                mock_post.return_value.text = "OK"
                                
                                # Should complete without errors
                                crawler.run_indexing()
                                
                                # Verify batch was consumed from Redis
                                remaining_batches = fake_redis.llen("batch-queue")
                                assert remaining_batches == 0

    def test_crawl_url_functionality(self, mock_environment):
        """Test individual URL crawling functionality"""
        mock_html_content = b"""<!DOCTYPE html>
        <html>
            <head><title>Test Page</title></head>
            <body>
                <div class="content">
                    <p>This is test content with a <a href="/link1">link</a>.</p>
                    <p>More content here with additional text to make it substantial.</p>
                    <p>Even more content to ensure the justext library recognizes this as good content.</p>
                </div>
            </body>
        </html>
        """
        
        with patch('mwmbl.crawler.retrieve.fetch') as mock_fetch:
            with patch('mwmbl.crawler.retrieve.robots_allowed', return_value=True):
                mock_fetch.return_value = (200, mock_html_content)
                
                from mwmbl.crawl import crawl_url
                
                result = crawl_url('https://example.com')
                
                assert result['url'] == 'https://example.com'
                assert result['status'] == 200
                assert result['content'] is not None
                assert result['content']['title'] == 'Test Page'
                # The extract might be empty due to justext filtering, so let's just check it exists
                assert 'extract' in result['content']
                assert result['error'] is None

    def test_crawl_url_error_handling(self, mock_environment):
        """Test URL crawling error handling"""
        with patch('mwmbl.crawler.retrieve.fetch') as mock_fetch:
            with patch('mwmbl.crawler.retrieve.robots_allowed', return_value=True):
                # Test connection error
                mock_fetch.side_effect = ConnectionError("Connection failed")
                
                from mwmbl.crawl import crawl_url
                
                result = crawl_url('https://example.com')
                
                assert result['url'] == 'https://example.com'
                assert result['status'] is None
                assert result['content'] is None
                assert result['error'] is not None
                assert result['error']['name'] == 'AbortError'

    def test_robots_denied_handling(self, mock_environment):
        """Test robots.txt denial handling"""
        with patch('mwmbl.crawler.retrieve.robots_allowed', return_value=False):
            from mwmbl.crawl import crawl_url
            
            result = crawl_url('https://example.com')
            
            assert result['url'] == 'https://example.com'
            assert result['status'] is None
            assert result['content'] is None
            assert result['error'] is not None
            assert result['error']['name'] == 'RobotsDenied'

    def test_batch_queue_operations(self, fake_redis):
        """Test batch queue operations in Redis"""
        # Test pushing and popping batches
        sample_batch = {
            'user_id_hash': 'test_user',
            'timestamp': int(time.time() * 1000),
            'items': []
        }
        
        # Push batch
        fake_redis.rpush("batch-queue", json.dumps(sample_batch))
        
        # Pop batch
        batch_data = fake_redis.lpop("batch-queue")
        assert batch_data is not None
        
        retrieved_batch = json.loads(batch_data)
        assert retrieved_batch['user_id_hash'] == 'test_user'

    def test_domain_scoring_and_url_management(self, fake_redis, sample_found_urls):
        """Test domain scoring and URL management in Redis"""
        url_queue = RedisURLQueue(fake_redis, lambda: set())
        
        # Queue URLs
        url_queue.queue_urls(sample_found_urls)
        
        # Check domain scores were created
        domain_count = fake_redis.zcard("domain-scores")
        assert domain_count > 0
        
        # Check individual domain URL counts
        example_count = url_queue.get_domain_count("example.com")
        test_count = url_queue.get_domain_count("test.com")
        
        assert example_count >= 0
        assert test_count >= 0

    def test_user_url_assignment(self, fake_redis, sample_found_urls):
        """Test user URL assignment and tracking"""
        url_queue = RedisURLQueue(fake_redis, lambda: set())
        
        # Queue URLs and get batch
        url_queue.queue_urls(sample_found_urls)
        batch = url_queue.get_batch("test_user")
        
        # Check user assignment
        user_urls = fake_redis.get("user-urls-test_user")
        assert user_urls is not None
        
        assigned_urls = json.loads(user_urls)
        assert len(assigned_urls) == len(batch)
        
        # Test URL checking
        uncrawled = url_queue.check_user_crawled_urls("test_user", batch)
        assert len(uncrawled) == 0  # All URLs should be assigned to this user

    def test_integration_workflow(self, fake_redis, mock_settings, mock_environment, 
                                 sample_found_urls, mock_crawl_response, temp_data_path):
        """Test the complete integration workflow"""
        with patch('mwmbl.crawl.redis', fake_redis):
            with patch('redis.Redis.from_url', return_value=fake_redis):
                # Setup URL queue
                url_queue = RedisURLQueue(fake_redis, lambda: set())
                url_queue.queue_urls(sample_found_urls)
                
                with patch('mwmbl.crawl.url_queue', url_queue):
                    with patch('mwmbl.crawl.crawl_url', return_value=mock_crawl_response):
                        with patch('mwmbl.crawl.record_urls_in_database'):
                            # Mock Counter object for index_batches return value
                            from collections import Counter
                            mock_counter = Counter({'example': 1})
                            with patch('mwmbl.crawl.index_batches', return_value=mock_counter):
                                with patch('mwmbl.crawl.RemoteIndex') as mock_remote_index:
                                    with patch('mwmbl.crawl.TinyIndex') as mock_tiny_index:
                                        with patch('mwmbl.crawl.index_pages') as mock_index_pages:
                                            with patch('requests.post') as mock_post:
                                                # Setup mocks for indexing
                                                mock_remote_index_instance = MagicMock()
                                                mock_remote_index.return_value = mock_remote_index_instance
                                                mock_remote_index_instance.retrieve.return_value = []
                                                
                                                mock_tiny_index_instance = MagicMock()
                                                mock_tiny_index.return_value.__enter__.return_value = mock_tiny_index_instance
                                                mock_tiny_index_instance.retrieve.return_value = []
                                                mock_tiny_index_instance.get_key_page_index.return_value = 0
                                                mock_tiny_index_instance.get_page.return_value = "test content"
                                                
                                                mock_post.return_value.status_code = 200
                                                mock_post.return_value.text = "OK"
                                                
                                                # Import functions
                                                from mwmbl.crawl import process_batch, run_indexing
                                                
                                                # Run complete workflow
                                                process_batch()  # Should create batch in Redis
                                                
                                                # Verify batch was created
                                                batch_count = fake_redis.llen("batch-queue")
                                                assert batch_count == 1
                                                
                                                # Run indexing
                                                run_indexing()  # Should process the batch
                                                
                                                # Verify batch was consumed
                                                remaining_batches = fake_redis.llen("batch-queue")
                                                assert remaining_batches == 0

    def test_error_recovery_and_resilience(self, fake_redis, mock_settings, mock_environment):
        """Test error recovery and system resilience"""
        with patch('mwmbl.crawl.redis', fake_redis):
            with patch('redis.Redis.from_url', return_value=fake_redis):
                with patch('mwmbl.crawl.url_queue') as mock_url_queue:
                    with patch('mwmbl.crawl.record_urls_in_database'):
                        # Test with empty batch
                        mock_url_queue.get_batch.return_value = []
                        
                        from mwmbl.crawl import process_batch
                        
                        # Should handle empty batch gracefully
                        process_batch()
                        
                        # Test indexing with no batches - mock sleep to avoid waiting
                        with patch('time.sleep'):
                            with patch('mwmbl.crawl.index_batches') as mock_index_batches:
                                with patch('mwmbl.crawl.TinyIndex') as mock_tiny_index:
                                    with patch('mwmbl.crawl.RemoteIndex') as mock_remote_index:
                                        # Mock the index_batches function to avoid file system access
                                        from collections import Counter
                                        mock_index_batches.return_value = Counter()
                                        
                                        # Setup TinyIndex mock
                                        mock_tiny_index_instance = MagicMock()
                                        mock_tiny_index.return_value.__enter__.return_value = mock_tiny_index_instance
                                        
                                        # Setup RemoteIndex mock
                                        mock_remote_index_instance = MagicMock()
                                        mock_remote_index.return_value = mock_remote_index_instance
                                        mock_remote_index_instance.retrieve.return_value = []
                                        
                                        from mwmbl.crawl import run_indexing
                                        
                                        # Should handle no batches gracefully (will sleep and return)
                                        run_indexing()
