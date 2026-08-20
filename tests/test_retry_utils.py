import os
import sys
import unittest


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from retry_utils import async_iter_with_retry, retry_async


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status code: {status_code}")
        self.status_code = status_code


class RetryUtilsTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_async_succeeds_after_transient_failures(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise StatusError(429)
            return "ok"

        result = await retry_async(
            operation,
            "test operation",
            max_attempts=3,
            interval_ms=0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)

    async def test_retry_async_does_not_retry_non_transient_status(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            raise StatusError(400)

        with self.assertRaises(StatusError):
            await retry_async(
                operation,
                "test operation",
                max_attempts=20,
                interval_ms=0,
            )

        self.assertEqual(attempts, 1)

    async def test_stream_retries_before_first_item(self):
        attempts = 0

        def factory():
            async def iterator():
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise StatusError(503)
                yield "chunk"

            return iterator()

        chunks = [
            item
            async for item in async_iter_with_retry(
                factory,
                "test stream",
                max_attempts=3,
                interval_ms=0,
            )
        ]

        self.assertEqual(chunks, ["chunk"])
        self.assertEqual(attempts, 3)

    async def test_stream_does_not_retry_after_first_item(self):
        attempts = 0

        def factory():
            async def iterator():
                nonlocal attempts
                attempts += 1
                yield "chunk"
                raise StatusError(503)

            return iterator()

        received = []
        with self.assertRaises(StatusError):
            async for item in async_iter_with_retry(
                factory,
                "test stream",
                max_attempts=20,
                interval_ms=0,
            ):
                received.append(item)

        self.assertEqual(received, ["chunk"])
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
