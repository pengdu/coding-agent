"""Demo fixture: deliberately small, deliberately broken.

Not real code. It exists so the scripted demo has a before and an after:
`test_add` passes now, `test_retry_call_retries_until_success` fails because
retry_call does not exist yet.
"""

import time

_CALLS = {"n": 0}


def add(a, b):
    return a + b


def fetch():
    """Fails the first two calls, then succeeds."""
    _CALLS["n"] += 1
    if _CALLS["n"] < 3:
        raise ConnectionError(f"transient failure #{_CALLS['n']}")
    return "payload"
