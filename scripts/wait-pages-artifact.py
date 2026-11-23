#!/usr/bin/env python3
"""Wait for this run's exact uploaded Pages artifact; never deploy or mutate it."""

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

ARTIFACT_NAME = 'github-pages'
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_WAIT_SECONDS = 120


class DeadlineExceeded(RuntimeError):
    """A fatal wall-clock deadline, deliberately distinct from socket timeouts."""


@contextmanager
def wall_clock_deadline(seconds):
    # This CLI runs in the main thread of an Ubuntu Actions job. Socket timeouts
    # alone do not stop DNS or a body that keeps making slow incremental progress.
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise DeadlineExceeded(f'Pages artifact wait exceeded {seconds} seconds; deployment stopped')

    signal.signal(signal.SIGALRM, expired)
    delay = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    try:
        signal.setitimer(signal.ITIMER_REAL, delay)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        remaining = (max(0.000001, previous_timer[0] - (time.monotonic() - started))
                     if previous_timer[0] else 0)
        signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


class RetryableError(RuntimeError):
    """A temporary read failure; the caller still enforces one total deadline."""

    def __init__(self, message, retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def rate_limit_delay(headers):
    headers = {key.lower(): str(value).strip() for key, value in (headers or {}).items()}
    now = time.time()
    delays = []
    after = headers.get('retry-after', '')
    if after.isdecimal():
        delays.append(int(after))
    elif after:
        try:
            date = parsedate_to_datetime(after)
            if date.tzinfo is not None:
                delays.append(max(0, date.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            pass
    reset = headers.get('x-ratelimit-reset', '')
    if headers.get('x-ratelimit-remaining') == '0' and reset.isdecimal():
        delays.append(max(0, int(reset) - now))
    return max(1, *delays) if delays else None


def positive_integer(value):
    return type(value) is int and value > 0


def matching_artifact(payload, expected_id, run_id):
    if not isinstance(payload, dict):
        raise ValueError('Invalid artifact inventory')
    artifacts, count = payload.get('artifacts'), payload.get('total_count')
    if (not isinstance(artifacts, list) or type(count) is not int or count < 0
            or count != len(artifacts) or count > 100):
        raise ValueError('Malformed or incomplete artifact inventory')
    if not artifacts:
        return None
    # deploy-pages itself requires exactly one artifact with the chosen name.
    if len(artifacts) != 1:
        raise ValueError('More than one Pages artifact exists for this run')
    artifact = artifacts[0]
    if not isinstance(artifact, dict) or artifact.get('name') != ARTIFACT_NAME:
        raise ValueError('Unexpected artifact name in filtered inventory')
    if not positive_integer(artifact.get('id')):
        raise ValueError('Invalid artifact ID')
    workflow_run = artifact.get('workflow_run')
    if (not isinstance(workflow_run, dict) or not positive_integer(workflow_run.get('id'))
            or workflow_run['id'] != run_id):
        raise ValueError('Artifact belongs to a different or unknown workflow run')
    if artifact.get('expired') is not False:
        raise ValueError('Artifact is expired or expiration state is unknown')
    if not positive_integer(artifact.get('size_in_bytes')):
        raise ValueError('Artifact has no valid nonzero size')
    # A stale listing from a previous attempt must never authorize its artifact.
    return artifact if artifact['id'] == expected_id else None


def fetch_inventory(url, token, timeout, opener=None):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': '2026-03-10',
    })
    try:
        open_request = opener or urllib.request.build_opener(NoRedirects).open
        with open_request(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        delay = rate_limit_delay(error.headers) if error.code in (403, 429) else None
        if error.code == 429 or (error.code == 403 and delay is not None):
            raise RetryableError(f'Artifact API temporarily returned HTTP {error.code}',
                                 delay if delay is not None else 60) from None
        if 500 <= error.code < 600:
            raise RetryableError(f'Artifact API temporarily returned HTTP {error.code}') from None
        # Permissions, invalid requests, redirects and missing runs do not become
        # a successful deployment or an unbounded retry loop.
        raise RuntimeError(f'Artifact API returned HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise RetryableError(f'Artifact API read failed ({type(error).__name__})') from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('Artifact API response exceeds the size limit')
    return json.loads(body)


def wait_for_artifact(fetch, expected_id, run_id, *, max_seconds=MAX_WAIT_SECONDS, interval=5,
                      clock=time.monotonic, sleep=time.sleep, log=print):
    if (not positive_integer(expected_id) or not positive_integer(run_id)
            or max_seconds <= 0 or interval <= 0):
        raise ValueError('Invalid artifact wait configuration')
    deadline = clock() + max_seconds
    previous = None
    attempts = 0
    while clock() < deadline:
        attempts += 1
        delay = interval
        try:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            payload = fetch(min(10, remaining))
            artifact = matching_artifact(payload, expected_id, run_id)
            signature = ((artifact['id'], artifact['size_in_bytes'], run_id) if artifact else None)
            if clock() >= deadline:
                break
            if signature is not None and signature == previous:
                log(f'Confirmed Pages artifact {expected_id} in run {run_id} '
                    f'({artifact["size_in_bytes"]} bytes; {attempts} checks).')
                return artifact
            previous = signature
            reason = ('awaiting a second consistent listing' if signature
                      else 'uploaded artifact ID is not visible yet')
        except RetryableError as error:
            previous = None
            reason = str(error)
            delay = max(interval, error.retry_after)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        log(f'Waiting for Pages artifact {expected_id}: {reason}.')
        sleep(min(delay, remaining))
    raise TimeoutError(f'Pages artifact {expected_id} was not consistently visible '
                       f'in run {run_id} within {max_seconds} seconds; deployment stopped')


def main():
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GITHUB_REPOSITORY')
    ids = [os.environ.get(key, '') for key in ('PAGES_ARTIFACT_ID', 'GITHUB_RUN_ID')]
    if any(not re.fullmatch(r'[1-9][0-9]*', value) for value in ids):
        raise ValueError('Missing or invalid uploaded artifact ID / workflow run ID')
    expected_id, run_id = map(int, ids)
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        raise ValueError('GH_TOKEN is required for artifact visibility checks')
    api = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    parsed = urllib.parse.urlsplit(api)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid GitHub API base URL')
    query = urllib.parse.urlencode({'name': ARTIFACT_NAME, 'per_page': 100})
    url = f'{api}/repos/{repository}/actions/runs/{run_id}/artifacts?{query}'
    with wall_clock_deadline(MAX_WAIT_SECONDS):
        wait_for_artifact(lambda timeout: fetch_inventory(url, token, timeout), expected_id, run_id)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f'::error::{error}', file=sys.stderr)
        sys.exit(1)
