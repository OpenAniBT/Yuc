"""Bounded public HTTPS reads. Every route verifies yuc.wiki's certificate."""

from __future__ import annotations

import datetime as dt
import http.client
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 16 * 1024 * 1024
HOST = "yuc.wiki"
# GitHub Pages' published address. Host, SNI and certificate checks remain HOST.
PAGES_ADDRESS = "185.199.108.153"


class FetchError(RuntimeError):
    pass


class PagesConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = socket.create_connection((PAGES_ADDRESS, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=HOST)
        except BaseException:
            sock.close()
            raise


def allowed_url(url: str) -> bool:
    value = urlsplit(url)
    return (value.scheme == "https" and value.hostname == HOST and value.port in {None, 443}
            and value.username is None and value.password is None and not value.query)


class Fetcher:
    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.next_request = 0.0
        self.route: str | None = None

    def get(self, url: str, *, allow_missing: bool = False, validators: dict | None = None) -> tuple[bytes, dict]:
        if not allowed_url(url):
            raise FetchError(f"URL outside public source scope: {url}")
        routes = ["github-pages"] if self.route == "github-pages" else ["dns", "github-pages"]
        errors = []
        for route in routes:
            for attempt in range(3):
                try:
                    body, info = self._request(url, route, allow_missing=allow_missing, validators=validators)
                    self.route = route
                    return body, info
                except (OSError, http.client.HTTPException, FetchError) as exc:
                    errors.append(f"{route}: {type(exc).__name__}: {exc}")
                    if isinstance(exc, ssl.SSLCertVerificationError):
                        break
                    if attempt < 2:
                        time.sleep(attempt + 1)
        raise FetchError(f"could not fetch {url}: {'; '.join(errors)}")

    def _request(self, url: str, route: str, *, allow_missing: bool, validators: dict | None = None, redirects: int = 0) -> tuple[bytes, dict]:
        if not allowed_url(url) or redirects > 3:
            raise FetchError(f"invalid redirect: {url}")
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + self.delay
        context = ssl.create_default_context()
        cls = PagesConnection if route == "github-pages" else http.client.HTTPSConnection
        conn = cls(HOST, timeout=30, context=context)
        try:
            path = urlsplit(url).path or "/"
            headers = {
                "User-Agent": "AniBT-YucArchive/1.0 (+https://github.com/AniBT-Net/Yuc)",
                "Accept": "text/html,application/atom+xml,application/xml,text/plain;q=0.9",
                "Accept-Encoding": "identity",
            }
            if validators:
                if validators.get("etag"):
                    headers["If-None-Match"] = validators["etag"]
                elif validators.get("lastModified"):
                    headers["If-Modified-Since"] = validators["lastModified"]
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = urljoin(url, response.getheader("Location") or "")
                return self._request(location, route, allow_missing=allow_missing, validators=validators, redirects=redirects + 1)
            if response.status != 200 and not (allow_missing and response.status == 404) and not (validators and response.status == 304):
                raise FetchError(f"HTTP {response.status}: {url}")
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise FetchError(f"response exceeds {MAX_BYTES} bytes: {url}")
            return payload, {
                "url": url, "httpStatus": response.status,
                "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "etag": response.getheader("ETag"), "lastModified": response.getheader("Last-Modified"),
                "contentType": response.getheader("Content-Type"),
                "transport": {"tlsVerified": True, "route": route, "connectAddress": PAGES_ADDRESS if route == "github-pages" else None},
            }
        finally:
            conn.close()
