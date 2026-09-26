import datetime as dt
import http.client
import signal
import ssl
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from yuc.fetch import FetchError, Fetcher, PagesConnection, _retry_after, allowed_url


URL = 'https://yuc.wiki/202610/'


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def io(self, cost, timeout):
        self.now += min(cost, timeout)
        if cost >= timeout:
            raise TimeoutError('fake socket timed out')


class FakeSocket:
    def __init__(self, timeout):
        self.timeout = timeout
        self.timeouts = [timeout]

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)


class FakeResponse:
    def __init__(self, status=200, body=b'ok', *, headers=None, chunks=None, read_cost=0,
                 length=None):
        self.status = status
        self.headers = headers or {}
        self.chunks = list(chunks) if chunks is not None else [body, b'']
        self.read_cost = read_cost
        self.length = length
        self.closed = False
        self.connection = None

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, size):
        conn = self.connection
        conn.clock.io(self.read_cost, conn.sock.timeout)
        chunk = self.chunks.pop(0) if self.chunks else b''
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        if self.length is not None:
            self.length -= len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, clock, host, timeout, context, plan):
        self.clock = clock
        self.host = host
        self.timeout = timeout
        self.context = context
        self.plan = plan
        self.sock = FakeSocket(timeout)
        self.requests = []
        self.closed = False

    def connect(self):
        self.clock.io(self.plan.get('connect_cost', 0), self.timeout)
        if 'error' in self.plan:
            raise self.plan['error']

    def request(self, method, path, *, headers):
        self.clock.io(self.plan.get('request_cost', 0), self.sock.timeout)
        self.requests.append((method, path, headers))

    def getresponse(self):
        self.clock.io(self.plan.get('header_cost', 0), self.sock.timeout)
        response = self.plan.get('response', FakeResponse())
        response.connection = self
        return response

    def close(self):
        self.closed = True


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.connections = []
        self.plans = {'dns': [], 'github-pages': []}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('yuc.fetch.time.monotonic', self.clock.monotonic))
        self.stack.enter_context(patch('yuc.fetch.time.sleep', self.clock.sleep))
        for route, target in [('dns', 'yuc.fetch.http.client.HTTPSConnection'),
                              ('github-pages', 'yuc.fetch.PagesConnection')]:
            self.stack.enter_context(patch(target, self.factory(route)))

    def factory(self, route):
        def make(host, timeout, context):
            plans = self.plans[route]
            plan = plans.pop(0) if plans else {}
            conn = FakeConnection(self.clock, host, timeout, context, plan)
            self.connections.append((route, conn))
            return conn
        return make

    def response(self, route='dns', **kwargs):
        value = FakeResponse(**kwargs)
        self.plans[route].append({'response': value})
        return value

    def test_success_preserves_bytes_validators_and_verified_tls(self):
        self.response(body=b'<html>source bytes</html>', headers={'ETag': 'new'})
        payload, info = Fetcher().get(URL, validators={'etag': 'old', 'lastModified': 'date'})
        self.assertEqual(payload, b'<html>source bytes</html>')
        self.assertEqual(info['etag'], 'new')
        self.assertEqual(info['transport'], {'tlsVerified': True, 'route': 'dns', 'connectAddress': None})
        route, conn = self.connections[0]
        self.assertEqual(conn.host, 'yuc.wiki')
        self.assertEqual(conn.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(conn.context.check_hostname)
        self.assertEqual(conn.requests[0][:2], ('GET', '/202610/'))
        headers = conn.requests[0][2]
        self.assertEqual(headers['If-None-Match'], 'old')
        self.assertNotIn('If-Modified-Since', headers)
        self.assertTrue(conn.closed)

    def test_preferred_pages_route_recovers_to_dns(self):
        fetcher = Fetcher()
        fetcher.route = 'github-pages'
        self.plans['github-pages'].append({'error': ConnectionResetError('unreachable')})
        self.response(body=b'recovered')
        self.assertEqual(fetcher.get(URL)[0], b'recovered')
        self.assertEqual([route for route, _ in self.connections], ['github-pages', 'dns'])
        self.assertEqual(fetcher.route, 'dns')
        fetcher.get(URL)
        self.assertEqual(self.connections[-1][0], 'dns')

    def test_certificate_failure_tries_other_verified_route_without_retrying_bad_route(self):
        self.plans['dns'].append({'error': ssl.SSLCertVerificationError('expired')})
        self.plans['github-pages'].append({'error': ConnectionResetError('transient')})
        self.response('github-pages', body=b'recovered')
        result = Fetcher().get(URL)
        self.assertEqual(result[0], b'recovered')
        self.assertEqual([route for route, _ in self.connections], ['dns', 'github-pages', 'github-pages'])
        self.assertTrue(all(conn.context.check_hostname for _, conn in self.connections))

    def test_two_certificate_failures_stop_without_retries(self):
        for route in self.plans:
            self.plans[route].append({'error': ssl.SSLCertVerificationError('expired')})
        with self.assertRaises(FetchError):
            Fetcher().get(URL)
        self.assertEqual(len(self.connections), 2)

    def test_transient_http_statuses_retry_and_recover(self):
        for status in [429, 500, 502, 503, 504]:
            with self.subTest(status=status):
                self.response(status=status)
                self.response('github-pages', body=b'recovered')
                before = len(self.connections)
                self.assertEqual(Fetcher(delay=0).get(URL)[0], b'recovered')
                self.assertEqual(len(self.connections) - before, 2)

    def test_deterministic_http_errors_are_not_retried(self):
        for status in [400, 401, 403, 404, 408, 410, 422]:
            with self.subTest(status=status):
                response = self.response(status=status)
                before = len(self.connections)
                with self.assertRaisesRegex(FetchError, f'HTTP {status}'):
                    Fetcher().get(URL)
                self.assertEqual(len(self.connections) - before, 1)
                self.assertTrue(response.closed)

    def test_missing_and_conditional_responses_keep_public_contract(self):
        self.response(status=404, body=b'not found')
        self.assertEqual(Fetcher().get(URL, allow_missing=True)[1]['httpStatus'], 404)
        self.response(status=304, body=b'')
        payload, info = Fetcher().get(URL, validators={'lastModified': 'yesterday'})
        self.assertEqual((payload, info['httpStatus']), (b'', 304))
        self.assertEqual(self.connections[-1][1].requests[0][2]['If-Modified-Since'], 'yesterday')
        self.response(status=304)
        with self.assertRaisesRegex(FetchError, 'HTTP 304'):
            Fetcher().get(URL)

    def test_retry_after_is_capped_and_failure_attempts_are_finite(self):
        for route in self.plans:
            for _ in range(3):
                self.response(route, status=429, headers={'Retry-After': '999999'})
        with self.assertRaisesRegex(FetchError, 'HTTP 429'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(len(self.connections), 6)
        self.assertLessEqual(max(self.clock.sleeps), 5)
        self.assertLess(self.clock.now, 50)

    def test_connect_timeouts_share_budget_across_routes_and_attempts(self):
        for route in self.plans:
            self.plans[route] = [{'connect_cost': 100} for _ in range(3)]
        with self.assertRaisesRegex(FetchError, 'time budget exhausted'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(self.clock.now, 50)
        self.assertEqual(len(self.connections), 5)
        self.assertEqual(self.connections[-1][1].timeout, 8.5)
        self.assertTrue(all(conn.closed for _, conn in self.connections))

    def test_wall_clock_timer_interrupts_blocked_dns_and_restores_handler(self):
        handlers = []
        previous = object()

        def install(signum, handler):
            handlers.append(handler)
            return previous

        def blocked_connect():
            # A DNS resolver or trickling header can ignore the socket timeout.
            # Deliver the timer callback without using real network or sleeps.
            handlers[0](signal.SIGALRM, None)

        with patch('yuc.fetch.signal.getitimer', return_value=(0.0, 0.0)), \
             patch('yuc.fetch.signal.signal', side_effect=install), \
             patch('yuc.fetch.signal.setitimer') as timer, \
             patch.object(FakeConnection, 'connect', side_effect=blocked_connect):
            with self.assertRaisesRegex(FetchError, 'time budget exhausted'):
                Fetcher().get(URL)
        self.assertEqual(timer.call_args_list[0].args, (signal.ITIMER_REAL, 50.0))
        self.assertEqual(timer.call_args_list[-1].args, (signal.ITIMER_REAL, 0))
        self.assertIs(handlers[-1], previous)
        self.assertTrue(self.connections[0][1].closed)

    def test_does_not_replace_an_existing_process_timer(self):
        with patch('yuc.fetch.signal.getitimer', return_value=(5.0, 1.0)), \
             patch('yuc.fetch.signal.signal') as install, \
             patch('yuc.fetch.signal.setitimer') as timer:
            self.assertEqual(Fetcher().get(URL)[0], b'ok')
        install.assert_not_called()
        timer.assert_not_called()

    def test_rate_limit_wait_uses_budget_before_opening_connection(self):
        fetcher = Fetcher()
        fetcher.next_request = 100
        with self.assertRaisesRegex(FetchError, 'time budget exhausted'):
            fetcher.get(URL)
        self.assertEqual(self.clock.now, 50)
        self.assertFalse(self.connections)

    def test_slow_continuous_body_cannot_reset_total_budget(self):
        response = self.response(chunks=[b'x'] * 100, read_cost=1)
        with self.assertRaisesRegex(FetchError, 'time budget exhausted'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(self.clock.now, 50)
        self.assertEqual(len(self.connections), 1)
        self.assertEqual(self.connections[0][1].sock.timeouts[-1], 1)
        self.assertTrue(response.closed)

    def test_redirects_share_body_budget_and_close_previous_responses(self):
        responses = []
        for index in range(3):
            response = FakeResponse(status=302, headers={'Location': f'/next{index}/'})
            responses.append(response)
            self.plans['dns'].append({'response': response, 'header_cost': 9})
        self.plans['dns'].append({'response': FakeResponse(chunks=[b'x'] * 10, read_cost=9),
                                  'header_cost': 9})
        with self.assertRaisesRegex(FetchError, 'time budget exhausted'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(self.clock.now, 50)
        self.assertEqual(len(self.connections), 4)
        self.assertTrue(all(response.closed for response in responses))

    def test_redirect_scope_and_count_are_finite_without_retries(self):
        for location in ['https://other.example/', 'http://yuc.wiki/',
                         'https://yuc.wiki:444/', 'https://user@yuc.wiki/',
                         'https://yuc.wiki/?secret=1']:
            with self.subTest(location=location):
                self.response(status=302, headers={'Location': location})
                before = len(self.connections)
                with self.assertRaisesRegex(FetchError, 'invalid redirect'):
                    Fetcher(delay=0).get(URL)
                self.assertEqual(len(self.connections) - before, 1)
        for _ in range(4):
            self.response(status=302, headers={'Location': '/loop/'})
        before = len(self.connections)
        with self.assertRaisesRegex(FetchError, 'invalid redirect'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(len(self.connections) - before, 4)

    def test_oversized_body_is_not_retried(self):
        self.response(body=b'x' * 9)
        with patch('yuc.fetch.MAX_BYTES', 8), self.assertRaisesRegex(FetchError, 'exceeds 8 bytes'):
            Fetcher(delay=0).get(URL)
        self.assertEqual(len(self.connections), 1)

    def test_truncated_content_length_is_retried_instead_of_archived(self):
        self.response(body=b'x', length=2)
        self.response('github-pages', body=b'complete', length=8)
        self.assertEqual(Fetcher(delay=0).get(URL)[0], b'complete')
        self.assertEqual(len(self.connections), 2)

    def test_exact_content_length_does_not_read_closed_socket_again(self):
        response = self.response(body=b'ok', length=2)
        self.assertEqual(Fetcher(delay=0).get(URL)[0], b'ok')
        self.assertEqual(response.chunks, [b''])


class TransportTests(unittest.TestCase):
    def test_pages_connection_keeps_source_host_sni_and_closes_failed_tls(self):
        raw = Mock()
        context = ssl.create_default_context()
        with patch('yuc.fetch.socket.create_connection', return_value=raw) as create, \
             patch.object(context, 'wrap_socket', side_effect=ssl.SSLCertVerificationError('bad')) as wrap:
            conn = PagesConnection('yuc.wiki', timeout=4, context=context)
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.connect()
            create.assert_called_once_with(('185.199.108.153', 443), 4)
            wrap.assert_called_once_with(raw, server_hostname='yuc.wiki')
            raw.close.assert_called_once()

    def test_retry_after_supports_dates_and_rejects_malformed_values(self):
        self.assertEqual(_retry_after('999999'), 5)
        self.assertEqual(_retry_after('-1'), 0)
        self.assertEqual(_retry_after('bad header'), 0)
        self.assertEqual(_retry_after(None), 0)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        self.assertEqual(_retry_after(future.strftime('%a, %d %b %Y %H:%M:%S GMT')), 5)
        self.assertEqual(_retry_after('Thu, 01 Jan 1970 00:00:00 GMT'), 0)

    def test_malformed_port_and_host_are_outside_scope(self):
        for url in ['https://yuc.wiki:bad/', 'https://[bad/', 'https://yuc.wiki:999999/']:
            self.assertFalse(allowed_url(url))
            with self.assertRaises(FetchError):
                Fetcher().get(url)


if __name__ == '__main__':
    unittest.main()
