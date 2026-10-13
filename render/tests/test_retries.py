"""Exercise the real console handlers with fake upstreams and no credentials."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from google.genai import errors as genai_errors

import api_helpers as helpers
import config
import http_options
import render_retry
from models import OpenAIRequest
from upstreams import cookie_proxy as cookie


def status_error(status):
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class RenderRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (helpers, cookie):
            self.stack.enter_context(patch.object(module, "get_retry_settings", return_value=(2, 0)))
        self.stack.enter_context(patch.object(helpers, "is_gemini_response_valid", return_value=True))
        self.stack.enter_context(patch.object(helpers, "convert_to_openai_format", return_value={"choices": []}))
        self.stack.enter_context(patch.object(helpers, "convert_chunk_to_openai", return_value="data: unique\n\n"))
        self.stack.enter_context(patch.object(helpers.app_state, "get_setting",
                                             side_effect=lambda key, default=None: 0.001 if key == "fake_streaming_interval" else default))
        self.retry_stat = self.stack.enter_context(patch.object(helpers.stats, "add_retry"))
        self.request = OpenAIRequest(model="gemini-3.8-flash", messages=[{"role": "user", "content": "test"}])
        self.connected = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def express(self, outcomes, stream=False):
        method = AsyncMock(side_effect=outcomes)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=method, generate_content_stream=method)))
        self.request.stream = stream
        response = await helpers.execute_gemini_call(client, self.request.model, lambda _: [], {}, self.request)
        if stream:
            response = [item async for item in response.body_iterator]
        return method, response

    async def test_success_makes_one_call(self):
        method, response = await self.express([SimpleNamespace()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(method.await_count, 1)
        self.retry_stat.assert_not_called()

    async def test_429_retries_then_succeeds(self):
        method, response = await self.express([status_error(429), SimpleNamespace()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(method.await_count, 2)
        self.retry_stat.assert_called_once()

    async def test_sdk_429_retries_then_succeeds(self):
        error = genai_errors.ClientError(429, {"error": {"message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        method, response = await self.express([error, SimpleNamespace()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(method.await_count, 2)

    async def test_exhausted_retries_stop_at_configured_attempt_count(self):
        method = AsyncMock(side_effect=[status_error(429) for _ in range(3)])
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=method)))
        with self.assertRaises(httpx.HTTPStatusError):
            await helpers.execute_gemini_call(client, self.request.model, lambda _: [], {}, self.request)
        self.assertEqual(method.await_count, 3)
        self.assertEqual(self.retry_stat.call_count, 2)

    async def test_invalid_request_and_authentication_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), self.assertRaises(httpx.HTTPStatusError):
                await self.express([status_error(status)])
        self.retry_stat.assert_not_called()

    async def test_real_stream_429_before_content_retries(self):
        async def success():
            yield SimpleNamespace()
        method, chunks = await self.express([status_error(429), success()], stream=True)
        self.assertEqual(method.await_count, 2)
        self.assertEqual(chunks.count("data: unique\n\n"), 1)
        self.assertIn("data: [DONE]\n\n", chunks)

    async def test_real_stream_empty_transport_error_before_content_retries(self):
        async def failure():
            raise httpx.ReadError("")
            yield
        async def success():
            yield SimpleNamespace()
        method, chunks = await self.express([failure(), success()], stream=True)
        self.assertEqual(method.await_count, 2)
        self.assertEqual(chunks.count("data: unique\n\n"), 1)

    async def test_real_stream_failure_after_content_does_not_replay(self):
        async def partial():
            yield SimpleNamespace()
            raise status_error(429)
        method, chunks = await self.express([partial()], stream=True)
        self.assertEqual(method.await_count, 1)
        self.assertEqual(chunks.count("data: unique\n\n"), 1)
        self.retry_stat.assert_not_called()

    async def test_fake_stream_429_retries(self):
        method = AsyncMock(side_effect=[status_error(429), SimpleNamespace()])
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=method)))
        chunks = [chunk async for chunk in helpers.gemini_fake_stream_generator(
            client, self.request.model, [], {}, self.request, False)]
        self.assertEqual(method.await_count, 2)
        self.assertIn("data: [DONE]\n\n", chunks)

    def cookie_setup(self):
        self.stack.enter_context(patch.object(cookie, "_get_cookie_string", return_value="test-cookie"))
        self.stack.enter_context(patch.object(cookie, "_get_project_id", return_value="test-project"))
        self.stack.enter_context(patch.object(cookie, "build_headers", return_value={"X-Test": "1"}))
        self.stack.enter_context(patch.object(cookie, "build_batch_graphql_body_async", AsyncMock(return_value={"variables": {}})))
        client = AsyncMock()
        client.__aenter__.return_value = client
        self.stack.enter_context(patch.object(cookie.httpx, "AsyncClient", return_value=client))
        return client

    async def test_cookie_nonstream_429_retries(self):
        client = self.cookie_setup()
        client.post.side_effect = [httpx.Response(429, text="busy"), httpx.Response(400, text="invalid request")]
        response = await cookie.CookieProxyUpstream().chat_completions(self.request, self.connected)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(client.post.await_count, 2)
        self.retry_stat.assert_called_once()

    async def test_cookie_stream_retries_before_content_and_never_replays(self):
        self.cookie_setup()
        self.request.stream = True
        attempts = 0
        async def upstream(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                yield "retryable_error", "HTTP 429"
            else:
                yield "text", "unique output"
                yield "retryable_error", "HTTP 429"
        self.stack.enter_context(patch.object(cookie, "_execute_stream_request_generator", upstream))
        response = await cookie.CookieProxyUpstream().chat_completions(self.request, self.connected)
        chunks = [item async for item in response.body_iterator]
        self.assertEqual(attempts, 2)
        self.assertEqual(sum("unique output" in item for item in chunks), 1)
        self.retry_stat.assert_called_once()

    async def test_cookie_image_empty_transport_error_retries(self):
        client = self.cookie_setup()
        client.post.side_effect = [httpx.ReadError(""), httpx.Response(400, text="invalid request")]
        result = await cookie._collect_full_response("test-project", self.request.model, self.request,
                                                     {}, {}, 2, 0, self.connected)
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(result["kind"], "error")
        self.retry_stat.assert_called_once()


class PolicySettingsTests(unittest.TestCase):
    def test_existing_render_environment_defaults(self):
        self.assertEqual(config.DEFAULT_SETTINGS["retry_max"], 19)
        self.assertEqual(config.DEFAULT_SETTINGS["retry_backoff_seconds"], 0.5)

    def test_sdk_does_not_add_hidden_retries(self):
        self.assertEqual(http_options.get_http_options().retry_options.attempts, 1)

    def test_disabled_retries_still_allow_one_attempt(self):
        with patch.object(render_retry, "RETRY_ENABLED", False):
            self.assertEqual(helpers.get_retry_settings()[0], 0)
            self.assertFalse(render_retry.is_retryable(status_error(429)))

    def test_console_settings_override_defaults(self):
        with patch.object(helpers.app_state, "get_setting", side_effect=lambda key, default: {
            "retry_max": 3, "retry_backoff_seconds": 2,
        }[key]):
            self.assertEqual(helpers.get_retry_settings(), (3, 2))

    def test_explicit_auth_status_overrides_quota_words(self):
        self.assertFalse(render_retry.is_retryable("HTTP 403: quota permission denied"))
        self.assertTrue(render_retry.is_retryable("RESOURCE_EXHAUSTED: try again later"))


if __name__ == "__main__":
    unittest.main()
