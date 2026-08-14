"""Tests for the HTTP fetching layer, including conditional requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from webmonitor.config import NetworkConfig
from webmonitor.core.fetcher import Fetcher, FetchError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
def network() -> NetworkConfig:
    """Provide network settings with retries disabled.

    Returns:
        A :class:`~webmonitor.config.NetworkConfig` for deterministic tests.
        HTTP/2 is off because respx's mock transport does not negotiate it.
    """
    return NetworkConfig(max_retries=0, timeout=5.0, http2=False)


@pytest.fixture
async def fetcher(network: NetworkConfig) -> AsyncIterator[Fetcher]:
    """Provide a fetcher that is closed on teardown.

    Args:
        network: Network configuration fixture.

    Yields:
        A ready :class:`~webmonitor.core.fetcher.Fetcher`.
    """
    async with Fetcher(network) as instance:
        yield instance


@respx.mock
async def test_fetches_body_and_validators(fetcher: Fetcher) -> None:
    """A 200 returns the body along with any cache validators."""
    respx.get("https://example.com").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>hello</body></html>",
            headers={"ETag": 'W/"v1"', "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
    )

    response = await fetcher.fetch("https://example.com")

    assert response.status_code == 200
    assert "hello" in response.text
    assert response.etag == 'W/"v1"'
    assert response.last_modified == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert response.bytes_transferred > 0
    assert response.not_modified is False


@respx.mock
async def test_sends_conditional_headers(fetcher: Fetcher) -> None:
    """Stored validators are replayed so the server can answer 304."""
    route = respx.get("https://example.com").mock(return_value=httpx.Response(304))

    await fetcher.fetch(
        "https://example.com",
        etag='W/"v1"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )

    request = route.calls.last.request
    assert request.headers["If-None-Match"] == 'W/"v1"'
    assert request.headers["If-Modified-Since"] == "Wed, 21 Oct 2015 07:28:00 GMT"


@respx.mock
async def test_304_transfers_no_body(fetcher: Fetcher) -> None:
    """The whole point of conditional requests: an empty, free response."""
    respx.get("https://example.com").mock(return_value=httpx.Response(304))

    response = await fetcher.fetch("https://example.com", etag='W/"v1"')

    assert response.not_modified is True
    assert response.bytes_transferred == 0
    assert response.text == ""
    # Validators are carried forward so the next poll stays conditional.
    assert response.etag == 'W/"v1"'


@respx.mock
async def test_client_error_raises(fetcher: Fetcher) -> None:
    """A 404 is a permanent failure and is not retried."""
    respx.get("https://example.com").mock(return_value=httpx.Response(404))

    with pytest.raises(FetchError, match="404"):
        await fetcher.fetch("https://example.com")


@respx.mock
async def test_server_error_retries_then_gives_up(network: NetworkConfig) -> None:
    """Retryable statuses are attempted the configured number of times."""
    network.max_retries = 2
    route = respx.get("https://example.com").mock(return_value=httpx.Response(503))

    async with Fetcher(network) as instance:
        with pytest.raises(FetchError):
            await instance.fetch("https://example.com")

    assert route.call_count == 3  # initial attempt plus two retries


@respx.mock
async def test_retry_succeeds_after_transient_failure(network: NetworkConfig) -> None:
    """A flaky server recovers without surfacing an error."""
    network.max_retries = 2
    respx.get("https://example.com").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, html="<p>recovered</p>"),
        ]
    )

    async with Fetcher(network) as instance:
        response = await instance.fetch("https://example.com")

    assert response.status_code == 200
    assert "recovered" in response.text


@respx.mock
async def test_network_error_is_wrapped(fetcher: Fetcher) -> None:
    """Transport failures become :class:`FetchError`, not raw httpx errors."""
    respx.get("https://example.com").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(FetchError):
        await fetcher.fetch("https://example.com")


@respx.mock
async def test_oversized_body_is_truncated(network: NetworkConfig) -> None:
    """A runaway response is capped rather than exhausting memory."""
    network.max_response_bytes = 2048
    respx.get("https://example.com").mock(return_value=httpx.Response(200, text="x" * 100_000))

    async with Fetcher(network) as instance:
        response = await instance.fetch("https://example.com")

    assert response.truncated is True
    # bytes_transferred is the real network cost; the retained text is capped.
    assert response.bytes_transferred == 100_000
    assert len(response.text) == 2048


@respx.mock
async def test_sets_user_agent(fetcher: Fetcher, network: NetworkConfig) -> None:
    """Some sites serve different markup to unknown agents."""
    route = respx.get("https://example.com").mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch("https://example.com")

    assert route.calls.last.request.headers["User-Agent"] == network.user_agent


@respx.mock
async def test_clients_are_pooled_per_proxy(fetcher: Fetcher) -> None:
    """Repeated fetches reuse one client so TLS handshakes are amortised."""
    respx.get("https://example.com").mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch("https://example.com")
    first = await fetcher._client_for("")
    await fetcher.fetch("https://example.com")
    second = await fetcher._client_for("")

    assert first is second


@respx.mock
async def test_decodes_declared_charset(fetcher: Fetcher) -> None:
    """A non-UTF-8 page is decoded using the charset the server declared."""
    body = "Grüße".encode("iso-8859-1")
    respx.get("https://example.com").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/html; charset=iso-8859-1"}
        )
    )

    response = await fetcher.fetch("https://example.com")

    assert "Grüße" in response.text
