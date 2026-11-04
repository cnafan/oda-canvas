"""HTTP client for URLs supplied at runtime by a portal admin.

Admin-only access is the real control here; this module limits what a mistaken
or malicious URL can reach. Names are resolved once and the connection is pinned
to the checked address, so DNS rebinding cannot swap it before the connection
opens, while TLS still verifies the original hostname. Private ranges stay
allowed because external registries often live on them, which means a literal
in-cluster 10.x address cannot be told apart and is not blocked.

The server on the other end is untrusted too, so every call has a wall-clock limit
and a limit on the bytes received, however slowly or compactly they arrive.
"""
import ipaddress
import socket
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

import httpx

TIMEOUT = httpx.Timeout(4.0, connect=2.0)
TOTAL_SECONDS = 5.0
RESOLVE_SECONDS = 2.0
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

_BLOCKED_SUFFIXES = (".localhost", ".svc", ".cluster.local")


class InvalidUrl(ValueError):
    """The URL is malformed or not allowed. The message is safe to show."""


class BlockedAddress(Exception):
    """The host points somewhere the portal refuses to send requests."""


class ResolveFailed(Exception):
    """The host name does not resolve."""


class UnusableResponse(Exception):
    """The response was too large, or compressed despite asking for it not to be."""


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def _check_host(host: str) -> None:
    """Refuse cluster-internal names and blocked literal addresses, without using DNS."""
    name = host.lower().rstrip(".")
    try:
        if _is_blocked_ip(name):
            raise BlockedAddress(host)
        return
    except ValueError:
        pass
    if name == "localhost" or name.endswith(_BLOCKED_SUFFIXES) or ".svc." in name or "." not in name:
        raise BlockedAddress(host)


def validate_url(url: str) -> httpx.URL:
    """Check an http(s) URL carries nothing but a location the portal may call."""
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError) as exc:
        raise InvalidUrl("The URL is not valid.") from exc
    if parsed.scheme not in ("http", "https"):
        raise InvalidUrl("The URL must start with http:// or https://.")
    if not parsed.host:
        raise InvalidUrl("The URL must include a host.")
    if parsed.userinfo or parsed.query or parsed.fragment:
        raise InvalidUrl("The URL must not include credentials, a query string or a fragment.")
    try:
        _check_host(parsed.host)
    except BlockedAddress as exc:
        raise InvalidUrl("The URL must not point at loopback, link-local or cluster-internal addresses.") from exc
    return parsed


def _resolve(host: str, port: Optional[int]) -> str:
    """Return an address for host, refusing the host if any of its addresses is blocked."""
    # getaddrinfo has no timeout and can't be interrupted, so it runs where the caller can
    # stop waiting. Each lookup gets its own thread, so a dead nameserver can't hold up
    # lookups for other registries, as it could in a shared pool.
    lookup: Future = Future()

    def run() -> None:
        try:
            lookup.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            lookup.set_exception(exc)

    threading.Thread(target=run, daemon=True, name="registry-resolve").start()
    try:
        records = lookup.result(timeout=RESOLVE_SECONDS)
    except (FutureTimeout, socket.gaierror, UnicodeError) as exc:
        raise ResolveFailed(host) from exc
    addresses = [record[4][0] for record in records]
    if not addresses:
        raise ResolveFailed(host)
    if any(_is_blocked_ip(address) for address in addresses):
        raise BlockedAddress(host)
    return addresses[0]


def _client(verify: bool) -> httpx.Client:
    return httpx.Client(verify=verify, trust_env=False, follow_redirects=False, timeout=TIMEOUT)


def send(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    verify: bool = True,
) -> httpx.Response:
    """Send one request to the checked address. Redirects are returned, never followed."""
    parsed = validate_url(url)
    pinned = parsed.copy_with(host=_resolve(parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80)))
    client = _client(verify)
    expired = threading.Event()

    def expire() -> None:
        # httpx's timeouts apply per read, so a server trickling bytes would never trip them.
        expired.set()
        client.close()

    watchdog = threading.Timer(TOTAL_SECONDS, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        with client.stream(
            method,
            pinned,
            headers={**(headers or {}), "Host": parsed.netloc.decode("ascii"), "Accept-Encoding": "identity"},
            params=params,
            data=data,
            extensions={"sni_hostname": parsed.host},
        ) as response:
            # Decoding could inflate a small body far past the limit, so only raw bytes are accepted.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise UnusableResponse(parsed.host)
            body = bytearray()
            for chunk in response.iter_raw():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise UnusableResponse(parsed.host)
            return httpx.Response(response.status_code, content=bytes(body), request=response.request)
    except Exception:
        if expired.is_set():
            raise httpx.ReadTimeout("response took too long") from None
        raise
    finally:
        watchdog.cancel()
        client.close()
