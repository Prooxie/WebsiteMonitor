"""Async HTTP fetching with conditional requests.

The single most effective optimisation in a change monitor is to not download
anything. Servers that support ``ETag`` or ``Last-Modified`` will answer a
conditional request with ``304 Not Modified`` and an empty body, so a poll of an
unchanged page costs one round trip and a few hundred header bytes instead of
the full document. On a page checked every five minutes that is the difference
between megabytes and kilobytes per hour.

On top of that:

* One :class:`httpx.AsyncClient` per proxy configuration, reused for the whole
  process, so TCP and TLS handshakes are amortised across checks.
* HTTP/2 when the server offers it, multiplexing several checks of the same host
  over one connection.
* Bodies are streamed and capped, so a watched URL that suddenly returns a
  gigabyte cannot exhaust memory.
* Retries use exponential backoff with jitter, and honour ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from types import TracebackType
from typing import Final

import httpx

from webmonitor.config import NetworkConfig

__all__ = ["FetchError", "FetchResponse", "Fetcher"]

logger = logging.getLogger(__name__)

#: Status codes worth retrying: rate limiting and transient server faults.
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Cap on honoured ``Retry-After``, so a hostile header cannot stall a worker.
MAX_RETRY_AFTER: Final[float] = 120.0


class FetchError(Exception):
    """A fetch failed after exhausting retries."""


@dataclass(slots=True, frozen=True)
class FetchResponse:
    """The result of one HTTP fetch.

    Attributes:
        status_code: Final HTTP status code.
        text: Decoded body. Empty when ``not_modified`` is set.
        etag: ``ETag`` response header, if any.
        last_modified: ``Last-Modified`` response header, if any.
        bytes_transferred: Body bytes actually read.
        elapsed_ms: Wall-clock duration including retries.
        not_modified: ``True`` when the server answered 304, meaning the caller
            already holds the current content.
        truncated: ``True`` if the body hit ``max_response_bytes`` and was cut
            short.
    """

    status_code: int
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    bytes_transferred: int = 0
    elapsed_ms: float = 0.0
    not_modified: bool = False
    truncated: bool = False


class Fetcher:
    """A pooled, retrying, conditional-request HTTP client.

    Use as an async context manager so the underlying clients are closed:

    .. code-block:: python

        async with Fetcher(settings.network) as fetcher:
            response = await fetcher.fetch("https://example.com", etag=stored_etag)
    """

    def __init__(self, config: NetworkConfig) -> None:
        """Initialise the fetcher.

        Args:
            config: Network settings controlling timeouts, retries and proxying.
        """
        self._config = config
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def _build_client(self, proxy: str) -> httpx.AsyncClient:
        """Create a client for a given proxy.

        Args:
            proxy: Proxy URL, or an empty string for a direct connection.

        Returns:
            A configured :class:`httpx.AsyncClient`.
        """
        limits = httpx.Limits(
            max_connections=self._config.max_connections,
            max_keepalive_connections=max(2, self._config.max_connections // 2),
            keepalive_expiry=60.0,
        )
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.timeout, connect=min(10.0, self._config.timeout)),
            follow_redirects=True,
            http2=self._config.http2,
            verify=self._config.verify_tls,
            limits=limits,
            proxy=proxy or None,
            headers={
                "User-Agent": self._config.user_agent,
                # Ask for compression; httpx transparently decodes the response.
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                # Explicitly defeat intermediary caches: a cached 200 would mask
                # a real change, which is precisely the bug we cannot ship.
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )

    async def _client_for(self, proxy: str) -> httpx.AsyncClient:
        """Return the pooled client for a proxy, creating it on first use.

        Args:
            proxy: Proxy URL, or an empty string for a direct connection.

        Returns:
            The shared client for that proxy.
        """
        key = proxy or ""
        client = self._clients.get(key)
        if client is not None:
            return client

        async with self._lock:
            # Re-check: another coroutine may have created it while we waited.
            client = self._clients.get(key)
            if client is None:
                client = self._build_client(key)
                self._clients[key] = client
                logger.debug("Created HTTP client for proxy %r", key or "<direct>")
        return client

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Read a ``Retry-After`` header expressed in seconds.

        Args:
            response: The response to inspect.

        Returns:
            The delay in seconds, clamped to :data:`MAX_RETRY_AFTER`, or ``None``
            if the header is absent or in the HTTP-date form we do not parse.
        """
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(float(raw), MAX_RETRY_AFTER)
        except ValueError:
            return None

    def _backoff_delay(self, attempt: int) -> float:
        """Compute the delay before a retry.

        Args:
            attempt: Zero-based attempt index that just failed.

        Returns:
            Seconds to wait: ``2 ** attempt`` with +/-25% jitter, so that many
            targets failing at once do not retry in lockstep.
        """
        base = min(2.0**attempt, 30.0)
        return base * random.uniform(0.75, 1.25)

    async def _read_capped(self, response: httpx.Response) -> tuple[str, int, bool]:
        """Stream a response body up to the configured size cap.

        Args:
            response: An open streaming response.

        Returns:
            A tuple of decoded text, bytes received from the wire, and whether
            the body was cut short at the cap. The byte count reflects what was
            actually transferred, which may exceed the cap when a single chunk
            overshoots it; the returned text never does.
        """
        cap = self._config.max_response_bytes
        buffer = bytearray()
        received = 0
        truncated = False

        async for chunk in response.aiter_bytes():
            received += len(chunk)
            buffer.extend(chunk)
            if len(buffer) >= cap:
                truncated = True
                # A chunk can straddle the cap - httpx hands us whatever the
                # transport produced, which for a small body is the lot. Trim
                # so the cap is a real bound on what we hold and parse, not
                # merely the point at which we stop asking for more.
                del buffer[cap:]
                logger.warning("Response from %s exceeded %d bytes; truncating", response.url, cap)
                break

        encoding = response.charset_encoding or "utf-8"
        try:
            text = buffer.decode(encoding, errors="replace")
        except LookupError:
            # Server declared a charset Python does not know.
            text = buffer.decode("utf-8", errors="replace")

        return text, received, truncated

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        proxy: str | None = None,
    ) -> FetchResponse:
        """Fetch a URL, using a conditional request when validators are supplied.

        Args:
            url: Absolute URL to fetch.
            etag: Previously stored ``ETag``, replayed as ``If-None-Match``.
            last_modified: Previously stored ``Last-Modified``, replayed as
                ``If-Modified-Since``.
            proxy: Per-target proxy URL, overriding the global setting.

        Returns:
            A :class:`FetchResponse`. A 304 comes back with ``not_modified`` set
            and an empty body.

        Raises:
            FetchError: If every attempt failed, or the server returned a
                non-retryable error status.
        """
        client = await self._client_for(proxy if proxy is not None else self._config.proxy)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        loop = asyncio.get_running_loop()
        started = loop.time()
        last_error: str = "unknown error"

        for attempt in range(self._config.max_retries + 1):
            try:
                request = client.build_request("GET", url, headers=headers)
                response = await client.send(request, stream=True)

                try:
                    if response.status_code == 304:
                        await response.aclose()
                        elapsed = (loop.time() - started) * 1000
                        logger.debug("304 Not Modified for %s (%.0f ms)", url, elapsed)
                        return FetchResponse(
                            status_code=304,
                            etag=etag,
                            last_modified=last_modified,
                            elapsed_ms=elapsed,
                            not_modified=True,
                        )

                    if response.status_code in RETRYABLE_STATUS:
                        retry_after = self._retry_after_seconds(response)
                        await response.aclose()
                        last_error = f"HTTP {response.status_code}"
                        if attempt < self._config.max_retries:
                            delay = retry_after or self._backoff_delay(attempt)
                            logger.info(
                                "%s from %s; retrying in %.1fs (attempt %d/%d)",
                                last_error,
                                url,
                                delay,
                                attempt + 1,
                                self._config.max_retries,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise FetchError(f"{url}: {last_error} after retries")

                    if response.status_code >= 400:
                        await response.aclose()
                        raise FetchError(f"{url}: HTTP {response.status_code}")

                    text, size, truncated = await self._read_capped(response)
                finally:
                    await response.aclose()

                elapsed = (loop.time() - started) * 1000
                logger.debug("Fetched %s: %d bytes in %.0f ms", url, size, elapsed)
                return FetchResponse(
                    status_code=response.status_code,
                    text=text,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    bytes_transferred=size,
                    elapsed_ms=elapsed,
                    truncated=truncated,
                )

            except FetchError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._config.max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.info(
                        "%s while fetching %s; retrying in %.1fs (attempt %d/%d)",
                        last_error,
                        url,
                        delay,
                        attempt + 1,
                        self._config.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            except httpx.HTTPError as exc:
                raise FetchError(f"{url}: {exc}") from exc

        raise FetchError(f"{url}: {last_error}")

    async def aclose(self) -> None:
        """Close every pooled client."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        logger.debug("Fetcher clients closed")

    async def __aenter__(self) -> Fetcher:
        """Enter the async context manager.

        Returns:
            This fetcher.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close pooled clients on context exit.

        Args:
            exc_type: Exception class, if one is propagating.
            exc: Exception instance, if one is propagating.
            tb: Traceback, if one is propagating.
        """
        await self.aclose()
