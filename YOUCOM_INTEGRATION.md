# You.com Search Integration for mwmbl

This integration adds You.com as an optional search source in mwmbl's Super Search system.

## Implementation

### Files Added/Modified

- **`mwmbl/tinysearchengine/super_search_sources/youcom.py`** - New You.com search adapter
- **`mwmbl/tinysearchengine/super_search_sources/__init__.py`** - Added youcom to SOURCES registry  
- **`test/test_youcom_source.py`** - Unit tests for You.com adapter

### How It Works

The You.com adapter follows mwmbl's existing search source pattern:

1. **Async HTTP Interface**: Implements `async def search(client, query, limit) -> list[Document]`
2. **Error Handling**: Graceful failure - returns empty list on HTTP/parse errors
3. **Authentication**: Optional API key via `YDC_API_KEY` environment variable
4. **Fallback**: Uses keyless endpoint (100 free searches/day) when no API key is set

### Configuration

#### With API Key (Recommended for Production)
```bash
export YDC_API_KEY=your_api_key_here
```

#### Without API Key (Keyless Tier)
No configuration needed - automatically uses keyless endpoint with IP-based rate limiting.

### Usage

Once integrated, You.com results appear alongside other search sources in mwmbl's Super Search results. The integration is completely optional and doesn't affect existing functionality.

### API Response Mapping

You.com Search API responses are mapped to mwmbl's Document format:
- `results.web[].title` → `Document.title`
- `results.web[].url` → `Document.url` 
- `results.web[].description` → `Document.extract`

### Error Handling

The adapter handles various failure modes:
- HTTP errors (timeout, connection failed)
- Authentication errors (invalid API key)
- Rate limiting (quota exceeded)
- Invalid JSON responses
- Missing required fields

All errors are logged and result in an empty result list, allowing other search sources to continue functioning.

### Testing

Run tests with:
```bash
python -m pytest test/test_youcom_source.py -v
```

Tests cover:
- Successful search with valid responses
- HTTP error handling
- Invalid JSON handling
- Result limit enforcement
- API key vs keyless operation