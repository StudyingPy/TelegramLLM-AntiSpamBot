from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import urljoin, urlparse, urlunparse

from .config import Settings
from .models import MessageFeatures, OGPreview


logger = logging.getLogger(__name__)


class UnsafeURL(ValueError):
    """Raised when a URL violates SSRF guardrails."""


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    url: str
    scheme: str
    host: str
    connect_host: str
    port: int
    path: str


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, connect_host: str, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._connect_host = connect_host

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_host, self.port),
            self.timeout,
            self.source_address,
        )
        _validate_public_ip(ipaddress.ip_address(self.sock.getpeername()[0]))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, connect_host: str, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._connect_host = connect_host

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._connect_host, self.port),
            self.timeout,
            self.source_address,
        )
        _validate_public_ip(ipaddress.ip_address(sock.getpeername()[0]))
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class OGHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
            return
        if tag.lower() != "meta":
            return

        key = (attr_map.get("property") or attr_map.get("name") or "").strip().lower()
        content = attr_map.get("content", "").strip()
        if key and content and key not in self.meta:
            self.meta[key] = html.unescape(content)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def should_fetch_og(features: MessageFeatures, settings: Settings) -> bool:
    if not settings.og_fetch_enabled or not features.has_preview_url:
        return False
    compact_text = "".join(char for char in features.clean_text if not char.isspace())
    return len(compact_text) <= settings.og_short_text_max_chars


async def fetch_og_for_features(
    features: MessageFeatures,
    settings: Settings,
) -> OGPreview | None:
    preview_url = next((link.url for link in features.links if link.source == "preview"), None)
    if not preview_url:
        return None

    try:
        return await asyncio.to_thread(
            fetch_og_preview,
            preview_url,
            timeout=settings.og_fetch_timeout_seconds,
            max_bytes=settings.og_fetch_max_bytes,
            max_text_chars=settings.og_fetch_max_text_chars,
            max_redirects=settings.og_fetch_max_redirects,
        )
    except (UnsafeURL, URLError, OSError, UnicodeError, http.client.HTTPException) as exc:
        logger.info("OG fetch skipped or failed for preview URL: %s", exc)
        return None


def fetch_og_preview(
    url: str,
    timeout: float,
    max_bytes: int,
    max_text_chars: int,
    max_redirects: int,
) -> OGPreview:
    original_url = url
    current_url = url

    for redirect_count in range(max_redirects + 1):
        endpoint = _resolve_public_endpoint(current_url)
        connection = _pinned_connection(endpoint, timeout=timeout)
        try:
            connection.request(
                "GET",
                endpoint.path,
                headers={
                    "Host": endpoint.host,
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "TelegramLLMAntiSpamBot/0.1",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(1024)
                if not location or redirect_count >= max_redirects:
                    raise UnsafeURL("redirect limit exceeded or location missing")
                current_url = urljoin(current_url, location)
                continue

            if response.status >= 400:
                raise URLError(f"unexpected OG fetch status {response.status}")

            content_type = response.getheader("Content-Type", "")
            if not _is_html_content_type(content_type):
                return OGPreview(url=original_url, final_url=endpoint.url)

            body = response.read(max_bytes + 1)
            truncated = len(body) > max_bytes
            html_text = body[:max_bytes].decode(_charset_from_content_type(content_type), "replace")
            return parse_og_html(
                html_text,
                original_url=original_url,
                final_url=endpoint.url,
                max_text_chars=max_text_chars,
                truncated=truncated,
            )
        finally:
            connection.close()

    raise UnsafeURL("redirect limit exceeded")


def _pinned_connection(endpoint: ResolvedEndpoint, timeout: float) -> http.client.HTTPConnection:
    if endpoint.scheme == "https":
        return PinnedHTTPSConnection(endpoint.connect_host, endpoint.host, endpoint.port, timeout)
    return PinnedHTTPConnection(endpoint.connect_host, endpoint.host, endpoint.port, timeout)


def _resolve_public_endpoint(url: str) -> ResolvedEndpoint:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL("only http(s) preview URLs are allowed")
    if not parsed.hostname:
        raise UnsafeURL("preview URL host is required")
    if parsed.username or parsed.password:
        raise UnsafeURL("userinfo in preview URL is not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise UnsafeURL("non-standard preview URL ports are not allowed")

    connect_host = _resolve_public_host(parsed.hostname, port)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    normalized_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )
    return ResolvedEndpoint(
        url=normalized_url,
        scheme=parsed.scheme,
        host=parsed.hostname,
        connect_host=connect_host,
        port=port,
        path=path,
    )


def _validate_public_http_url(url: str) -> str:
    return _resolve_public_endpoint(url).url


def _resolve_public_host(host: str, port: int) -> str:
    try:
        ip = ipaddress.ip_address(host)
        _validate_public_ip(ip)
        return str(ip)
    except ValueError:
        pass

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise UnsafeURL("preview URL host cannot be resolved")

    public_addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        _validate_public_ip(ip)
        public_addresses.append(address)

    return public_addresses[0]


def parse_og_html(
    html_text: str,
    original_url: str,
    final_url: str,
    max_text_chars: int,
    truncated: bool = False,
) -> OGPreview:
    parser = OGHTMLParser()
    parser.feed(html_text)

    title = _first_meta(parser, "og:title", "twitter:title") or _clean_text(" ".join(parser.title_parts))
    description = _first_meta(parser, "og:description", "twitter:description", "description")
    site_name = _first_meta(parser, "og:site_name", "application-name")
    image_alt = _first_meta(parser, "og:image:alt", "twitter:image:alt")

    parts = [title, description, site_name, image_alt]
    text = _clean_text(" ".join(part for part in parts if part))[:max_text_chars]
    return OGPreview(
        url=original_url,
        final_url=final_url,
        title=title,
        description=description,
        site_name=site_name,
        image_alt=image_alt,
        text=text,
        truncated=truncated or len(text) >= max_text_chars,
    )


def _validate_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UnsafeURL("preview URL resolves to a non-public address")


def _is_html_content_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return not lowered or "text/html" in lowered or "application/xhtml+xml" in lowered


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _first_meta(parser: OGHTMLParser, *keys: str) -> str | None:
    for key in keys:
        value = parser.meta.get(key)
        if value:
            return _clean_text(value)
    return None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()
