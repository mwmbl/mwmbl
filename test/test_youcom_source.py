"""Tests for You.com search source integration."""
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from mwmbl.tinysearchengine.super_search_sources.youcom import search
from mwmbl.tinysearchengine.indexer import Document


class TestYouComSource:
    """Test You.com search source adapter."""

    @pytest.mark.asyncio
    async def test_search_with_valid_response(self):
        """Test successful search with valid You.com response."""
        mock_response = {
            "results": {
                "web": [
                    {
                        "title": "Test Result 1",
                        "url": "https://example.com/1",
                        "description": "This is a test result from You.com"
                    },
                    {
                        "title": "Test Result 2", 
                        "url": "https://example.com/2",
                        "description": "Another test result"
                    }
                ]
            }
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value.json.return_value = mock_response
        mock_client.get.return_value.raise_for_status.return_value = None
        
        results = await search(mock_client, "test query", 10)
        
        assert len(results) == 2
        assert isinstance(results[0], Document)
        assert results[0].title == "Test Result 1"
        assert results[0].url == "https://example.com/1"
        assert results[0].extract == "This is a test result from You.com"
        
    @pytest.mark.asyncio  
    async def test_search_with_http_error(self):
        """Test handling of HTTP errors."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPError("Connection failed")
        
        results = await search(mock_client, "test query", 10)
        
        assert results == []
        
    @pytest.mark.asyncio
    async def test_search_with_invalid_response(self):
        """Test handling of invalid JSON response."""
        mock_client = AsyncMock()
        mock_client.get.return_value.json.side_effect = ValueError("Invalid JSON")
        
        results = await search(mock_client, "test query", 10)
        
        assert results == []
        
    @pytest.mark.asyncio
    async def test_search_respects_limit(self):
        """Test that search respects the limit parameter."""
        # Mock response with 5 results
        mock_response = {
            "results": {
                "web": [
                    {"title": f"Result {i}", "url": f"https://example.com/{i}", "description": f"Description {i}"}
                    for i in range(5)
                ]
            }
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value.json.return_value = mock_response
        mock_client.get.return_value.raise_for_status.return_value = None
        
        results = await search(mock_client, "test query", 3)
        
        assert len(results) == 3  # Should respect the limit
        
    @pytest.mark.asyncio
    async def test_search_with_api_key(self):
        """Test that API key is used when available."""
        with patch.dict('os.environ', {'YDC_API_KEY': 'test_key'}):
            mock_client = AsyncMock()
            mock_client.get.return_value.json.return_value = {"results": {"web": []}}
            mock_client.get.return_value.raise_for_status.return_value = None
            
            await search(mock_client, "test query", 10)
            
            # Check that X-API-Key header was set
            call_args = mock_client.get.call_args
            headers = call_args[1]['headers']
            assert 'X-API-Key' in headers
            assert headers['X-API-Key'] == 'test_key'
            
    @pytest.mark.asyncio
    async def test_search_without_api_key(self):
        """Test keyless operation when no API key is set."""
        with patch.dict('os.environ', {}, clear=True):
            mock_client = AsyncMock()
            mock_client.get.return_value.json.return_value = {"results": {"web": []}}
            mock_client.get.return_value.raise_for_status.return_value = None
            
            await search(mock_client, "test query", 10)
            
            # Check that no X-API-Key header was set
            call_args = mock_client.get.call_args
            headers = call_args[1]['headers']
            assert 'X-API-Key' not in headers
            assert headers['User-Agent'] == 'youdotcom-integration/mwmbl-mwmbl'