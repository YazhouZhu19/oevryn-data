import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/wait-pages-artifact.py'
SPEC = importlib.util.spec_from_file_location('wait_pages_artifact', SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
RUN = 45012071862
ARTIFACT = 14750710244


def inventory():
    # Shape and values observed in the real failed run's later-visible artifact.
    return {'total_count': 1, 'artifacts': [{
        'id': ARTIFACT, 'name': 'github-pages', 'size_in_bytes': 37043613,
        'expired': False, 'workflow_run': {'id': RUN},
    }]}


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class VisibilityTests(unittest.TestCase):
    def wait(self, values, *, maximum=120):
        clock, calls = Clock(), []
        iterator = iter(values)

        def fetch(timeout):
            calls.append(timeout)
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return value

        result = gate.wait_for_artifact(fetch, ARTIFACT, RUN, max_seconds=maximum,
                                       clock=clock.time, sleep=clock.sleep, log=lambda _: None)
        return result, clock, calls

    def test_real_upload_then_empty_listing_then_visible_is_bounded(self):
        result, clock, calls = self.wait([
            {'total_count': 0, 'artifacts': []}, inventory(), inventory(),
        ])
        self.assertEqual(result['id'], ARTIFACT)
        self.assertEqual(clock.sleeps, [5, 5])
        self.assertEqual(len(calls), 3)

    def test_requires_two_consistent_listings_even_when_immediately_visible(self):
        _, clock, calls = self.wait([inventory(), inventory()])
        self.assertEqual(clock.sleeps, [5])
        self.assertEqual(len(calls), 2)

    def test_transient_api_failure_resets_consistency(self):
        _, clock, calls = self.wait([inventory(), gate.RetryableError('temporary 503'),
                                     inventory(), inventory()])
        self.assertEqual(len(calls), 4)
        self.assertEqual(clock.sleeps, [5, 5, 5])

    def test_wrong_uploaded_id_cannot_authorize_an_older_artifact(self):
        stale = inventory()
        stale['artifacts'][0]['id'] -= 1
        _, _, calls = self.wait([stale, inventory(), inventory()])
        self.assertEqual(len(calls), 3)

    def test_permanently_missing_or_wrong_id_stops_at_deadline(self):
        wrong = inventory()
        wrong['artifacts'][0]['id'] -= 1
        for value in [{'total_count': 0, 'artifacts': []}, wrong]:
            with self.subTest(value=value):
                clock, calls = Clock(), []

                def fetch(timeout):
                    calls.append(timeout)
                    return value

                with self.assertRaisesRegex(TimeoutError, 'deployment stopped'):
                    gate.wait_for_artifact(fetch, ARTIFACT, RUN, max_seconds=12,
                                           clock=clock.time, sleep=clock.sleep, log=lambda _: None)
                self.assertEqual(clock.now, 12)
                self.assertEqual(clock.sleeps, [5, 5, 2])
                self.assertEqual(calls, [10, 7, 2])

    def test_response_after_deadline_cannot_pass(self):
        clock, calls = Clock(), []

        def fetch(timeout):
            calls.append(timeout)
            clock.now += 8
            return inventory()

        with self.assertRaises(TimeoutError):
            gate.wait_for_artifact(fetch, ARTIFACT, RUN, max_seconds=20,
                                   clock=clock.time, sleep=clock.sleep, log=lambda _: None)
        self.assertEqual(len(calls), 2)

    def test_malformed_expired_foreign_duplicate_and_empty_artifacts_fail_immediately(self):
        cases = [None, {}, {'total_count': 1, 'artifacts': []}]
        for field, value in [('name', 'other'), ('expired', True), ('expired', None),
                             ('size_in_bytes', 0), ('size_in_bytes', True),
                             ('id', '14750710244'), ('workflow_run', {'id': RUN + 1}),
                             ('workflow_run', None)]:
            item = inventory()
            item['artifacts'][0][field] = value
            cases.append(item)
        duplicate = inventory()
        duplicate['artifacts'].append(copy.deepcopy(duplicate['artifacts'][0]))
        duplicate['total_count'] = 2
        cases.append(duplicate)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                gate.matching_artifact(item, ARTIFACT, RUN)

    def test_fatal_api_errors_are_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.wait([RuntimeError('Artifact API returned HTTP 403')])

    def test_rate_limit_wait_is_honored_without_exceeding_deadline(self):
        clock = Clock()
        calls = []

        def fetch(timeout):
            calls.append(timeout)
            raise gate.RetryableError('temporary 429', retry_after=600)

        with self.assertRaises(TimeoutError):
            gate.wait_for_artifact(fetch, ARTIFACT, RUN, max_seconds=120,
                                   clock=clock.time, sleep=clock.sleep, log=lambda _: None)
        self.assertEqual(clock.sleeps, [120])
        self.assertEqual(len(calls), 1)

    def test_rate_limit_reset_does_not_allow_an_early_retry(self):
        clock, calls = Clock(), []

        def fetch(timeout):
            calls.append(clock.now)
            if len(calls) == 1:
                raise gate.RetryableError('temporary 403', retry_after=27)
            return inventory()

        result = gate.wait_for_artifact(fetch, ARTIFACT, RUN, max_seconds=60,
                                        clock=clock.time, sleep=clock.sleep, log=lambda _: None)
        self.assertEqual(result['id'], ARTIFACT)
        self.assertEqual(calls, [0, 27, 32])


@unittest.skipUnless(hasattr(signal, 'setitimer'), 'Requires the Unix Actions runtime')
class DeadlineTests(unittest.TestCase):
    def test_slow_incremental_socket_read_is_stopped_by_wall_clock_deadline(self):
        reader, writer = socket.socketpair()
        reader.settimeout(0.5)
        stop = threading.Event()

        def drip():
            try:
                while not stop.wait(0.005):
                    writer.sendall(b' ')
            except OSError:
                pass

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        started = time.monotonic()
        previous_handler = signal.getsignal(signal.SIGALRM)
        try:
            with self.assertRaises(gate.DeadlineExceeded):
                with gate.wall_clock_deadline(0.05):
                    gate.fetch_inventory('https://api.github.com/example', 'test-only', 10,
                                         lambda *args, **kwargs: reader.makefile('rb'))
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(signal.getsignal(signal.SIGALRM), previous_handler)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        finally:
            stop.set()
            reader.close()
            writer.close()
            thread.join(timeout=1)

    def test_existing_handler_and_timer_are_restored(self):
        handler = signal.getsignal(signal.SIGALRM)
        timer = signal.getitimer(signal.ITIMER_REAL)
        previous = lambda *_args: None
        try:
            signal.signal(signal.SIGALRM, previous)
            signal.setitimer(signal.ITIMER_REAL, 10, 2)
            with gate.wall_clock_deadline(1):
                time.sleep(0.01)
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 8)
            self.assertLess(remaining, 10)
            self.assertEqual(interval, 2)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, handler)
            signal.setitimer(signal.ITIMER_REAL, *timer)

    def test_absolute_deadline_is_not_reclassified_as_retryable_socket_timeout(self):
        def blocked_fetch(_timeout):
            time.sleep(2)
            return inventory()

        with self.assertRaises(gate.DeadlineExceeded):
            with gate.wall_clock_deadline(0.02):
                gate.wait_for_artifact(blocked_fetch, ARTIFACT, RUN, log=lambda _: None)


class ApiTests(unittest.TestCase):
    def test_request_uses_documented_headers_and_returns_inventory(self):
        def opener(request, timeout):
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.get_header('X-github-api-version'), '2026-03-10')
            self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-only')
            self.assertEqual(timeout, 10)
            return io.BytesIO(json.dumps(inventory()).encode())

        actual = gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
        self.assertEqual(actual, inventory())

    def test_transient_statuses_and_network_errors_are_classified_without_leaking_body(self):
        for error in [urllib.error.HTTPError('url', 503, 'secret body', {}, None),
                      urllib.error.HTTPError('url', 429, 'secret body', {'Retry-After': '20'}, None),
                      urllib.error.URLError('secret connection detail'), TimeoutError('secret')]:
            with self.subTest(error=error):
                def opener(*args, **kwargs):
                    raise error
                with self.assertRaises(gate.RetryableError) as caught:
                    gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
                self.assertNotIn('secret', str(caught.exception))
                if getattr(error, 'code', None) == 429:
                    self.assertEqual(caught.exception.retry_after, 20)

    def test_auth_and_other_non_transient_http_errors_are_fatal(self):
        for code in [301, 302, 400, 401, 403, 404]:
            with self.subTest(code=code):
                def opener(*args, **kwargs):
                    raise urllib.error.HTTPError('url', code, 'secret', {}, None)
                with self.assertRaisesRegex(RuntimeError, f'HTTP {code}') as caught:
                    gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
                self.assertNotIsInstance(caught.exception, gate.RetryableError)

    def test_only_explicit_rate_limited_403_is_retryable(self):
        with patch.object(gate.time, 'time', return_value=1000):
            for headers, delay in [({'Retry-After': '20'}, 20),
                                   ({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1035'}, 35),
                                   ({'Retry-After': '10', 'X-RateLimit-Remaining': '0',
                                     'X-RateLimit-Reset': '1040'}, 40)]:
                with self.subTest(headers=headers):
                    def opener(*args, **kwargs):
                        raise urllib.error.HTTPError('url', 403, 'rate limited', headers, None)
                    with self.assertRaises(gate.RetryableError) as caught:
                        gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
                    self.assertEqual(caught.exception.retry_after, delay)
            for headers in [{}, {'Retry-After': 'invalid'},
                            {'x-ratelimit-remaining': '1', 'x-ratelimit-reset': '1035'},
                            {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': 'invalid'}]:
                with self.subTest(headers=headers):
                    def opener(*args, **kwargs):
                        raise urllib.error.HTTPError('url', 403, 'forbidden', headers, None)
                    with self.assertRaises(RuntimeError) as caught:
                        gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
                    self.assertNotIsInstance(caught.exception, gate.RetryableError)

    def test_429_honors_later_reset_or_retry_after_including_http_date(self):
        with patch.object(gate.time, 'time', return_value=1000):
            for headers, expected in [({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1090'}, 90),
                                      ({'Retry-After': '100', 'x-ratelimit-remaining': '0',
                                        'x-ratelimit-reset': '1090'}, 100),
                                      ({'Retry-After': 'Thu, 01 Jan 1970 00:17:20 GMT'}, 40),
                                      ({}, 60)]:
                with self.subTest(headers=headers):
                    def opener(*args, **kwargs):
                        raise urllib.error.HTTPError('url', 429, 'rate limited', headers, None)
                    with self.assertRaises(gate.RetryableError) as caught:
                        gate.fetch_inventory('https://api.github.com/example', 'test-only', 10, opener)
                    self.assertEqual(caught.exception.retry_after, expected)

    def test_redirects_cannot_forward_the_authorization_header(self):
        self.assertIsNone(gate.NoRedirects().redirect_request(None, None, 302, '', {},
                                                            'https://other.example'))

    def test_oversized_or_non_json_api_responses_fail(self):
        for body in [b'x' * (gate.MAX_RESPONSE_BYTES + 1), b'not json']:
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                gate.fetch_inventory('https://api.github.com/example', 'test-only', 10,
                                     lambda *args, **kwargs: io.BytesIO(body))

    def test_main_binds_expected_upload_id_and_current_run(self):
        environment = {'GITHUB_REPOSITORY': 'YazhouZhu19/oevryn-data',
                       'GITHUB_RUN_ID': str(RUN), 'PAGES_ARTIFACT_ID': str(ARTIFACT),
                       'GH_TOKEN': 'test-only', 'GITHUB_API_URL': 'https://api.github.com'}
        with patch.dict(os.environ, environment, clear=True), patch.object(gate, 'wait_for_artifact') as wait, \
                patch.object(gate, 'wall_clock_deadline', wraps=gate.wall_clock_deadline) as deadline:
            gate.main()
            deadline.assert_called_once_with(120)
            self.assertEqual(wait.call_args.args[1:], (ARTIFACT, RUN))
            with patch.object(gate, 'fetch_inventory', return_value=inventory()) as fetch:
                wait.call_args.args[0](7)
                url, token, timeout = fetch.call_args.args
                self.assertIn(f'/actions/runs/{RUN}/artifacts?', url)
                self.assertIn('name=github-pages', url)
                self.assertIn('per_page=100', url)
                self.assertEqual((token, timeout), ('test-only', 7))

    def test_missing_or_invalid_upload_output_stops_before_network(self):
        for value in ['', 'null', '0', '-1', '123abc']:
            env = {'GITHUB_REPOSITORY': 'YazhouZhu19/oevryn-data',
                   'GITHUB_RUN_ID': str(RUN), 'PAGES_ARTIFACT_ID': value, 'GH_TOKEN': 'test-only'}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                gate.main()


if __name__ == '__main__':
    unittest.main()
