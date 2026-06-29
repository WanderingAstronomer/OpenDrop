"""Polite HTTP for the batch scrapers.

Adds three things on top of a plain ``httpx.Client`` so a nationwide, tens-of-thousands-of-request
sweep stays a good citizen and survives a long overnight run:

1. **Inter-request pacing** — never issues two requests on one client closer than ``delay_s`` apart.
2. **Exponential backoff with jitter** — retries transient failures (timeouts, connection resets,
   HTTP 429, and 5xx) instead of dropping the item on the first hiccup.
3. **Retry-After** — honors the server's own backoff hint when present.

All knobs are env-tunable (see ``.env.example``) so an operator can slow the sweep down further.
This module is **batch-only** — the API never imports it.
"""
from __future__ import annotations

import logging
import os
import random
import time

import httpx

log = logging.getLogger("opendrop.http")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


# Minimum seconds between requests on a single client (politeness pacing). 0 disables pacing.
REQUEST_DELAY_S = _env_float("SCRAPER_REQUEST_DELAY_S", 0.5)
# Retries on transient failures (timeouts, connection errors, HTTP 429, HTTP 5xx).
MAX_RETRIES = _env_int("SCRAPER_MAX_RETRIES", 4)
# Backoff schedule: wait = min(CAP, BASE * 2**(attempt-1)) + jitter.
BACKOFF_BASE_S = _env_float("SCRAPER_BACKOFF_BASE_S", 2.0)
BACKOFF_CAP_S = _env_float("SCRAPER_BACKOFF_CAP_S", 60.0)
# OSM Nominatim's usage policy is a hard maximum of 1 request/second — geocoding callers pin at
# least this regardless of the global delay, so we never violate it even if the global delay is 0.
NOMINATIM_MIN_DELAY_S = 1.0


class PoliteClient:
    """An ``httpx.Client`` wrapper that paces and retries.

    Use as a context manager::

        with PoliteClient(timeout=30, headers={...}) as client:
            r = client.get(url, params=...)
            r.raise_for_status()

    A *persistent* failure (transient errors that outlast ``max_retries``) is re-raised, so the
    caller's per-item ``try/except`` can skip just that ZIP / grid-cell without aborting the sweep.
    A non-retryable 4xx (other than 429) is returned as-is for the caller to handle.
    """

    def __init__(self, *, timeout: float = 30, headers: dict | None = None,
                 delay_s: float | None = None, max_retries: int | None = None):
        self._delay = REQUEST_DELAY_S if delay_s is None else max(0.0, delay_s)
        self._max_retries = MAX_RETRIES if max_retries is None else max_retries
        self._client = httpx.Client(timeout=timeout, headers=headers or {})
        self._last = 0.0  # monotonic timestamp of the last request (0 => no pacing on first call)

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> bool:
        self._client.close()
        return False

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        if self._delay <= 0 or self._last == 0.0:
            return
        gap = self._delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        resp = getattr(exc, "response", None)
        if resp is None:
            return None
        raw = (resp.headers.get("Retry-After") or "").strip()
        if raw.isdigit():
            return float(raw)
        return None

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            self._pace()
            try:
                resp = self._client.request(method, url, **kwargs)
                self._last = time.monotonic()
                # Funnel 429 + 5xx into the same retry/backoff path as network errors.
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except httpx.HTTPError as exc:
                self._last = time.monotonic()
                attempt += 1
                if attempt > self._max_retries:
                    raise
                wait = self._retry_after_seconds(exc)
                if wait is None:
                    wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
                    wait += random.uniform(0, wait * 0.25)  # de-correlating jitter
                log.warning("%s %s attempt %d/%d failed (%s); backing off %.1fs",
                            method, url, attempt, self._max_retries, exc, wait)
                time.sleep(wait)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)
