"""Fail-closed outbound HTTP boundary for Collector source adapters.

The policy validates the logical URL, resolves every hostname, pins the request to
an approved public address, and repeats the whole process for every redirect.
Adapters never receive a raw ``httpx.AsyncClient`` and therefore cannot bypass
this boundary accidentally.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urljoin

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FORBIDDEN_CALLER_HEADERS = frozenset(
    {"connection", "cookie", "host", "proxy-authorization"}
)
_SENSITIVE_HEADERS = frozenset({"authorization", "cj-access-token", "x-api-key"})


class OutboundRequestError(RuntimeError):
    """Stable, non-secret error raised by the outbound boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OutboundPolicyError(OutboundRequestError):
    """The request target violates the configured source policy."""


class OutboundTransportError(OutboundRequestError):
    """The approved public target could not be reached."""


@dataclass(frozen=True, slots=True)
class OutboundPolicy:
    """Immutable allowlist and resource limits for one source adapter."""

    allowed_hosts: frozenset[str]
    allowed_ports: frozenset[int] = frozenset({443})
    max_redirects: int = 3
    max_response_bytes: int = 2 * 1024 * 1024
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        hosts = frozenset(_canonical_host(item) for item in self.allowed_hosts)
        if not hosts or any(not item for item in hosts):
            raise ValueError("at least one explicit host is required")
        if not self.allowed_ports or any(port < 1 or port > 65535 for port in self.allowed_ports):
            raise ValueError("allowed ports must be valid")
        if self.max_redirects < 0 or self.max_response_bytes <= 0:
            raise ValueError("redirect and response limits must be non-negative")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        object.__setattr__(self, "allowed_hosts", hosts)


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    logical_url: httpx.URL
    host: str
    port: int
    addresses: tuple[IPAddress, ...]


@dataclass(frozen=True, slots=True)
class SafeHttpResponse:
    """Bounded response detached from the underlying network connection."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> object:
        try:
            return httpx.Response(self.status_code, content=self.content).json()
        except ValueError as exc:
            raise OutboundRequestError(
                "invalid_json",
                "source returned an invalid JSON document",
            ) from exc


async def system_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve TCP addresses without blocking the event loop."""

    def resolve() -> tuple[str, ...]:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        return tuple(row[4][0] for row in rows)

    try:
        return await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise OutboundTransportError(
            "dns_resolution_failed",
            "source hostname could not be resolved",
            retryable=True,
        ) from exc


def _canonical_host(value: str) -> str:
    candidate = value.strip().rstrip(".").lower()
    if not candidate:
        return ""
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("host name is invalid") from exc


def _is_allowed_host(host: str, allowed_hosts: frozenset[str]) -> bool:
    return host in allowed_hosts


def _is_public_address(address: IPAddress) -> bool:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_private
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if address.ipv4_mapped is not None and not _is_public_address(address.ipv4_mapped):
            return False
    return True


def _public_addresses(values: Sequence[str]) -> tuple[IPAddress, ...]:
    addresses: list[IPAddress] = []
    seen: set[IPAddress] = set()
    for value in values:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise OutboundPolicyError(
                "invalid_dns_answer",
                "source hostname resolved to an invalid address",
            ) from exc
        if not _is_public_address(address):
            raise OutboundPolicyError(
                "non_public_address",
                "source hostname resolved to a non-public address",
            )
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise OutboundTransportError(
            "dns_resolution_failed",
            "source hostname did not resolve to an address",
            retryable=True,
        )
    return tuple(addresses)


async def validate_outbound_url(
    raw_url: str,
    *,
    policy: OutboundPolicy,
    resolver: Resolver = system_resolver,
) -> ValidatedTarget:
    """Validate one URL and return its complete approved DNS answer set."""

    if not raw_url or any(ord(character) < 32 for character in raw_url):
        raise OutboundPolicyError("invalid_url", "source URL is invalid")
    try:
        parsed = httpx.URL(raw_url)
    except (TypeError, ValueError) as exc:
        raise OutboundPolicyError("invalid_url", "source URL is invalid") from exc
    if not parsed.is_absolute_url or parsed.scheme.lower() != "https":
        raise OutboundPolicyError("https_required", "source URL must use HTTPS")
    if parsed.userinfo:
        raise OutboundPolicyError("url_credentials_forbidden", "source URL credentials are forbidden")
    if parsed.fragment:
        raise OutboundPolicyError("url_fragment_forbidden", "source URL fragments are forbidden")

    host = _canonical_host(parsed.host)
    if not host or not _is_allowed_host(host, policy.allowed_hosts):
        raise OutboundPolicyError("host_not_allowed", "source hostname is not allowed")
    port = parsed.port or 443
    if port not in policy.allowed_ports:
        raise OutboundPolicyError("port_not_allowed", "source port is not allowed")

    normalized_url = parsed.copy_with(host=host)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = _public_addresses(await resolver(host, port))
    else:
        addresses = _public_addresses((str(literal),))
    return ValidatedTarget(normalized_url, host, port, addresses)


class SafeHttpClient:
    """Small GET-only client that pins DNS and manually validates redirects."""

    def __init__(
        self,
        policy: OutboundPolicy,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._transport = transport

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> SafeHttpResponse:
        request_headers = _validated_headers(headers or {})
        current_url = url
        previous_host: str | None = None
        timeout = httpx.Timeout(
            connect=self._policy.connect_timeout_seconds,
            read=self._policy.read_timeout_seconds,
            write=self._policy.read_timeout_seconds,
            pool=self._policy.connect_timeout_seconds,
        )
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
            transport=self._transport,
            http2=False,
        ) as client:
            for redirect_count in range(self._policy.max_redirects + 1):
                target = await validate_outbound_url(
                    current_url,
                    policy=self._policy,
                    resolver=self._resolver,
                )
                confirmed = await self._confirm_dns(target)
                if previous_host is not None and target.host != previous_host:
                    request_headers = {
                        name: value
                        for name, value in request_headers.items()
                        if name.lower() not in _SENSITIVE_HEADERS
                    }
                response = await self._send_pinned(client, confirmed, request_headers)
                if response.status_code not in _REDIRECT_STATUSES:
                    return response
                location = response.headers.get("location")
                if not location:
                    return response
                if redirect_count >= self._policy.max_redirects:
                    raise OutboundPolicyError(
                        "too_many_redirects",
                        "source exceeded the redirect limit",
                    )
                previous_host = target.host
                current_url = urljoin(str(target.logical_url), location)
        raise AssertionError("redirect loop exhausted unexpectedly")

    async def _confirm_dns(self, target: ValidatedTarget) -> ValidatedTarget:
        try:
            ipaddress.ip_address(target.host)
        except ValueError:
            pass
        else:
            return target
        confirmed = _public_addresses(await self._resolver(target.host, target.port))
        if set(confirmed) != set(target.addresses):
            raise OutboundPolicyError(
                "dns_answer_changed",
                "source DNS answer changed during validation",
            )
        return ValidatedTarget(target.logical_url, target.host, target.port, confirmed)

    async def _send_pinned(
        self,
        client: httpx.AsyncClient,
        target: ValidatedTarget,
        headers: Mapping[str, str],
    ) -> SafeHttpResponse:
        address = target.addresses[0]
        pinned_url = target.logical_url.copy_with(host=address.compressed)
        outbound_headers = dict(headers)
        outbound_headers["Host"] = _host_header(target.host, target.port)
        # Do not reuse an IP-keyed TLS connection for a different logical host.
        outbound_headers["Connection"] = "close"
        request = client.build_request("GET", pinned_url, headers=outbound_headers)
        request.extensions["sni_hostname"] = target.host
        try:
            response = await client.send(request, stream=True)
            try:
                content = await _read_bounded(response, self._policy.max_response_bytes)
                return SafeHttpResponse(
                    url=str(target.logical_url),
                    status_code=response.status_code,
                    headers=MappingProxyType(dict(response.headers.items())),
                    content=content,
                )
            finally:
                await response.aclose()
        except OutboundRequestError:
            raise
        except httpx.TimeoutException as exc:
            raise OutboundTransportError(
                "source_timeout",
                "source request timed out",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise OutboundTransportError(
                "source_unreachable",
                "source request failed",
                retryable=True,
            ) from exc


def _validated_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        normalized = name.strip().lower()
        if not normalized or normalized in _FORBIDDEN_CALLER_HEADERS:
            raise OutboundPolicyError("forbidden_header", "source request contains a forbidden header")
        if "\r" in value or "\n" in value:
            raise OutboundPolicyError("invalid_header", "source request contains an invalid header")
        result[name] = value
    return result


def _host_header(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return rendered if port == 443 else f"{rendered}:{port}"


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise OutboundPolicyError(
                    "response_too_large",
                    "source response exceeds the configured size limit",
                )
        except ValueError as exc:
            raise OutboundPolicyError(
                "invalid_content_length",
                "source returned an invalid content length",
            ) from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise OutboundPolicyError(
                "response_too_large",
                "source response exceeds the configured size limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)