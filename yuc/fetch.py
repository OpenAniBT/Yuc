"""Bounded public HTTPS reads. Every route verifies yuc.wiki's certificate."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import http.client
import signal
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 16 * 1024 * 1024
REQUEST_BUDGET = 50.0
IO_TIMEOUT = 10.0
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 5.0
READ_CHUNK = 64 * 1024
HOST = "yuc.wiki"
# GitHub Pages' published address. Host, SNI and certificate checks remain HOST.
PAGES_ADDRESS = "185.199.108.153"


class FetchError(RuntimeError):
    pass


class _BudgetExpired(FetchError):
    pass


class _TransientResponse(FetchError):
    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


@contextmanager
def _wall_clock_limit(seconds: float):
    # Actions and the CLI run in the main thread on POSIX. This also interrupts
    # libc DNS and trickling header/chunk lines that socket idle timeouts cannot
    # bound. Do not take ownership of another caller's existing process timer.
    can_arm = (hasattr(signal, "setitimer")
               and threading.current_thread() is threading.main_thread()
               and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
    if not can_arm:
        yield
        return

    def expired(signum, frame):
        raise _BudgetExpired("request time budget exhausted")

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _BudgetExpired("request time budget exhausted")
    return remaining


def _pause(delay: float, deadline: float) -> None:
    remaining = _remaining(deadline)
    if delay > 0:
        time.sleep(min(delay, remaining))
    _remaining(deadline)


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = int(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            seconds = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, min(MAX_RETRY_AFTER, seconds))


class PagesConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = socket.create_connection((PAGES_ADDRESS, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=HOST)
        except BaseException:
            sock.close()
            raise


def allowed_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.hostname == HOST and value.port in {None, 443}
                and value.username is None and value.password is None and not value.query)
    except ValueError:
        return False


class Fetcher:
    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.next_request = 0.0
        self.route: str | None = None

    def get(self, url: str, *, allow_missing: bool = False, validators: dict | None = None) -> tuple[bytes, dict]:
        if not allowed_url(url):
            raise FetchError(f"URL outside public source scope: {url}")
        with _wall_clock_limit(REQUEST_BUDGET):
            return self._get(url, allow_missing=allow_missing, validators=validators)

    def _get(self, url: str, *, allow_missing: bool, validators: dict | None) -> tuple[bytes, dict]:
        deadline = time.monotonic() + REQUEST_BUDGET
        preferred = self.route or "dns"
        routes = [preferred, "dns" if preferred == "github-pages" else "github-pages"]
        errors = []
        rejected = set()
        try:
            for attempt in range(MAX_ATTEMPTS):
                if attempt:
                    _pause(attempt * 0.5, deadline)
                for route in routes:
                    if route in rejected:
                        continue
                    _remaining(deadline)
                    try:
                        body, info = self._request(url, route, deadline=deadline,
                                                   allow_missing=allow_missing, validators=validators)
                        self.route = route
                        return body, info
                    except (OSError, http.client.HTTPException, _TransientResponse) as exc:
                        errors.append(f"{route}: {type(exc).__name__}: {exc}")
                        # A certificate failure never becomes trusted through retries.
                        if isinstance(exc, ssl.SSLCertVerificationError):
                            rejected.add(route)
                        elif isinstance(exc, _TransientResponse):
                            _pause(exc.retry_after, deadline)
                if len(rejected) == len(routes):
                    break
        except _BudgetExpired as exc:
            errors.append(str(exc))
        raise FetchError(f"could not fetch {url}: {'; '.join(errors)}")

    def _request(self, url: str, route: str, *, deadline: float, allow_missing: bool,
                 validators: dict | None = None, redirects: int = 0) -> tuple[bytes, dict]:
        if not allowed_url(url) or redirects > 3:
            raise FetchError(f"invalid redirect: {url}")
        _pause(max(0, self.next_request - time.monotonic()), deadline)
        self.next_request = time.monotonic() + self.delay
        context = ssl.create_default_context()
        cls = PagesConnection if route == "github-pages" else http.client.HTTPSConnection
        conn = cls(HOST, timeout=min(IO_TIMEOUT, _remaining(deadline)), context=context)
        response = None
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
            conn.connect()
            # Keep the socket reference: getresponse can detach a closing socket.
            sock = conn.sock
            sock.settimeout(min(IO_TIMEOUT, _remaining(deadline)))
            conn.request("GET", path, headers=headers)
            sock.settimeout(min(IO_TIMEOUT, _remaining(deadline)))
            response = conn.getresponse()
            _remaining(deadline)
            if response.status in {301, 302, 303, 307, 308}:
                location = urljoin(url, response.getheader("Location") or "")
                response.close()
                conn.close()
                return self._request(location, route, deadline=deadline, allow_missing=allow_missing,
                                     validators=validators, redirects=redirects + 1)
            if response.status == 429 or 500 <= response.status <= 599:
                raise _TransientResponse(f"HTTP {response.status}: {url}",
                                         _retry_after(response.getheader("Retry-After")))
            if response.status != 200 and not (allow_missing and response.status == 404) and not (validators and response.status == 304):
                raise FetchError(f"HTTP {response.status}: {url}")
            payload = bytearray()
            while True:
                sock.settimeout(min(IO_TIMEOUT, _remaining(deadline)))
                chunk = response.read1(min(READ_CHUNK, MAX_BYTES + 1 - len(payload)))
                _remaining(deadline)
                if not chunk:
                    # read1 permits EOF before Content-Length without raising.
                    if response.length:
                        raise http.client.IncompleteRead(bytes(payload), response.length)
                    break
                payload.extend(chunk)
                if len(payload) > MAX_BYTES:
                    raise FetchError(f"response exceeds {MAX_BYTES} bytes: {url}")
                if response.length == 0:
                    break
            return bytes(payload), {
                "url": url, "httpStatus": response.status,
                "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "etag": response.getheader("ETag"), "lastModified": response.getheader("Last-Modified"),
                "contentType": response.getheader("Content-Type"),
                "transport": {"tlsVerified": True, "route": route, "connectAddress": PAGES_ADDRESS if route == "github-pages" else None},
            }
        finally:
            if response is not None:
                response.close()
            conn.close()
