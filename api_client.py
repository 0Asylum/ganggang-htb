import requests
import logging

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


class RateLimitError(Exception):
    """Raised when HTB responds with 429 (rate limited) to a request."""
    pass


class ApiClient:
    """Thin wrapper around a requests.Session for calling HTB's API: builds
    versioned URLs, attaches auth/user-agent headers, and normalizes error
    handling so callers get either a parsed JSON body or None.
    """

    def __init__(self, base_url, token=None, user_agent=None, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.session = requests.Session()

    def set_token(self, token):
        """Replaces the bearer token used for subsequent requests."""
        self.token = token

    def _build_url(self, path, api_version, base_url=None):
        """Builds a full request URL, e.g. `{base}/api/{version}/{path}`, or
        `{base}/{path}` if api_version is falsy.
        """
        base = (base_url or self.base_url).rstrip("/")
        path = path.lstrip("/")
        if api_version:
            return f"{base}/api/{api_version}/{path}"
        return f"{base}/{path}"

    def _build_headers(self, extra_headers=None):
        """Builds the default request headers (user agent, Accept, bearer
        token if set), merged with any request-specific overrides.
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def request(self, method, path, api_version="v5", params=None, json_body=None,
                headers=None, base_url=None):
        """Issues an HTTP request and returns the parsed JSON body, an empty
        dict for an empty response, or None on any failure (HTTP error,
        network error, or non-JSON body) -- except a 429, which raises
        RateLimitError instead of returning None, since that needs different
        handling (backoff) from a genuine failure.
        """
        url = self._build_url(path, api_version, base_url=base_url)
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                headers=self._build_headers(headers),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                logger.warning(f"{method.upper()} {url} rate limited (429)")
                raise RateLimitError(url) from exc
            logger.error(f"{method.upper()} {url} failed: {exc}")
            return None
        except requests.exceptions.RequestException as exc:
            logger.error(f"{method.upper()} {url} failed: {exc}")
            return None

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError:
            logger.warning(f"{method.upper()} {url} returned a non-JSON response")
            return None

    def get(self, path, **kwargs):
        """Shorthand for request("GET", path, ...)."""
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        """Shorthand for request("POST", path, ...)."""
        return self.request("POST", path, **kwargs)
