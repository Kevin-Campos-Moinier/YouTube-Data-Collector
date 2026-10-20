"""Minimal JSON-over-HTTPS GET with retries.

Transient failures (HTTP 429/5xx, network errors, and any platform error
reason the caller lists as retryable) are retried with exponential backoff and
jitter. Everything else surfaces as `HttpError` for the source to interpret.
Credentials go in `headers`, never in the URL; error messages never include the URL anyway.
"""

import http.client
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = f"socialcollect/{__version__} (academic research)"


class HttpError(Exception):
    """A non-retryable (or retries exhausted) HTTP failure.

    Attributes:
        status: HTTP status, 0 for network errors.
        reason: Platform error reason (e.g. "commentsDisabled"), or "" if none.
        body: Parsed JSON error body, {} if unavailable.
    """

    def __init__(self, status: int, reason: str, body: dict | None = None):
        super().__init__(f"HTTP {status} {reason}".strip())
        self.status, self.reason, self.body = status, reason, body or {}


def _reason(body: dict) -> str:
    err = body.get("error") or {}
    if not isinstance(err, dict):
        return str(err)
    errors = err.get("errors") or []
    if errors and errors[0].get("reason"):
        return errors[0]["reason"]
    return err.get("status") or ""


def get_json(url: str, params: dict, *, headers: dict | None = None,
             retry_reasons: frozenset = frozenset(), retries: int = 6, timeout: float = 30.0) -> dict:
    """GETs `url?params` and returns the decoded JSON body.

    Args:
        url: Endpoint without query string.
        params: Query parameters (values are str-converted).
        headers: Extra request headers, e.g. the API key.
        retry_reasons: Platform error reasons that are transient (e.g. a
            per-user rate limit reported as HTTP 403).
        retries: Attempts after the first one.
        timeout: Socket timeout in seconds.

    Raises:
        HttpError: On a non-retryable error, or once retries are exhausted.
    """
    full = url + "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT,
                                                "Accept-Encoding": "identity", **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            err = HttpError(e.code, _reason(body), body)
            if e.code not in RETRY_STATUS and err.reason not in retry_reasons:
                raise err from None
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as e:
            err = HttpError(0, type(e).__name__)
        if attempt == retries:
            raise err
        wait = min(64, 2 ** attempt) * random.uniform(0.5, 1.0)
        log.warning("%s; retrying in %.1fs (%d/%d)", err, wait, attempt + 1, retries)
        time.sleep(wait)
    raise AssertionError("unreachable")
