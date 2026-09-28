"""Imports are inside the test bodies on purpose.

A module-level `from utils import retry_call` would fail at collection, so
the broken run would report one error instead of a passing test plus a
failing one. We want the second state: green baseline, one clear failure.
"""

import unittest


class TestUtils(unittest.TestCase):
    def test_add(self):
        from utils import add

        self.assertEqual(add(2, 3), 5)

    def test_retry_call_retries_until_success(self):
        from utils import fetch, retry_call

        self.assertEqual(retry_call(fetch, times=5), "payload")


if __name__ == "__main__":
    unittest.main()
