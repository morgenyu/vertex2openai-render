import asyncio
import time
import unittest
from unittest.mock import patch

import httpx
from proxy_pool import ProxyPool, ProxyPoolUnavailable, is_proxy_retryable


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = ProxyPool()
        self.pool.controller = "http://controller"
        self.pool.batch_size = 1
        self.selected = "old"
        self.dead = {"dead"}
        self.probes = []
        self.selections = []

        async def controller(request):
            path = request.url.path
            if path == "/version":
                return httpx.Response(200, json={"version": "test"})
            if path == "/providers/proxies/subscription":
                return httpx.Response(200, json={"proxies": [
                    {"name": name, "type": kind} for name, kind in
                    [("old", "Vless"), ("dead", "Vless"), ("good", "Vless"), ("DIRECT", "Direct")]
                ]})
            if path == "/proxies/VERTEX-OUT":
                if request.method == "PUT":
                    import json
                    self.selected = json.loads(request.content)["name"]
                    self.selections.append(self.selected)
                    return httpx.Response(204)
                return httpx.Response(200, json={"now": self.selected, "all": ["old", "dead", "good", "DIRECT"]})
            if path.startswith("/providers/proxies/subscription/") and path.endswith("/healthcheck"):
                name = path.split("/")[-2]
                self.probes.append(name)
                self.assertEqual(request.url.params["expected-status"], "204")
                return httpx.Response(503 if name in self.dead else 200,
                                      json={"message": "offline"} if name in self.dead else {"delay": 12})
            raise AssertionError(f"Unexpected controller operation: {request.method} {path}")

        self.transport = httpx.MockTransport(controller)
        self.pool._client = lambda: httpx.AsyncClient(base_url=self.pool.controller, transport=self.transport)
        self.shuffle = patch("proxy_pool.random.shuffle", lambda names: None)
        self.shuffle.start()
        self.addCleanup(self.shuffle.stop)

    async def test_start_keeps_healthy_selection(self):
        await self.pool.start()
        self.assertEqual(self.pool.current, "old")
        self.assertEqual(self.probes, ["old"])

    async def test_failure_skips_dead_and_cools_failed_node(self):
        await self.pool.start()
        await self.pool.rotate(self.pool.generation)
        self.assertEqual(self.selected, "good")
        self.assertEqual(self.probes, ["old", "dead", "good"])
        self.assertGreater(self.pool.cooldowns["old"], time.monotonic())
        self.assertGreater(self.pool.cooldowns["dead"], time.monotonic())

    async def test_concurrent_old_failures_only_rotate_once(self):
        await self.pool.start()
        generation = self.pool.generation
        await asyncio.gather(*(self.pool.rotate(generation) for _ in range(12)))
        self.assertEqual(self.selections, ["old", "good"])

    async def test_no_healthy_node_fails_without_direct_fallback(self):
        await self.pool.start()
        self.dead.add("good")
        with self.assertRaises(ProxyPoolUnavailable):
            await self.pool.rotate(self.pool.generation)
        self.assertEqual(self.selected, "old")
        self.assertNotIn("DIRECT", self.probes)

    async def test_expired_cooldown_allows_recovery(self):
        await self.pool.start()
        await self.pool.rotate(self.pool.generation)
        self.pool.cooldowns["old"] = time.monotonic() - 1
        await self.pool.rotate(self.pool.generation)
        self.assertEqual(self.selected, "old")

    async def test_disabled_pool_performs_no_network_operations(self):
        self.pool.controller = ""
        await self.pool.start()
        await self.pool.rotate(0)
        self.assertEqual(self.probes, [])

    def test_error_classification(self):
        for code in (408, 429, 500, 502, 503, 504):
            response = httpx.Response(code, request=httpx.Request("GET", "https://example.com"))
            self.assertTrue(is_proxy_retryable(httpx.HTTPStatusError("", request=response.request, response=response)))
        for code in (400, 401, 403, 404):
            response = httpx.Response(code, request=httpx.Request("GET", "https://example.com"))
            self.assertFalse(is_proxy_retryable(httpx.HTTPStatusError("quota 429", request=response.request, response=response)))
        self.assertTrue(is_proxy_retryable(httpx.ReadError("")))
        self.assertFalse(is_proxy_retryable(ProxyPoolUnavailable("No node available")))


if __name__ == "__main__":
    unittest.main()
