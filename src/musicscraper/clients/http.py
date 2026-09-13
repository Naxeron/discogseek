"""
Resilient HTTP session creation with shared retry and user-agent defaults.
"""

from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from musicscraper.config import Config


def create_resilient_session(
    user_agent: Optional[str] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Creates a requests.Session with standard retry adapter and User-Agent."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": user_agent or Config.USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8"
    })
    return session
