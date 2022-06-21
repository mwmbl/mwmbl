import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


MAX_RETRY = 2
MAX_RETRY_FOR_SESSION = 2
BACK_OFF_FACTOR = 0.3
TIME_BETWEEN_RETRIES = 1000
ERROR_CODES = (500, 502, 504)


retry_requests = requests.Session()
retry = Retry(total=5, backoff_factor=1)
adapter = HTTPAdapter(max_retries=retry)
retry_requests.mount('http://', adapter)
retry_requests.mount('https://', adapter)
