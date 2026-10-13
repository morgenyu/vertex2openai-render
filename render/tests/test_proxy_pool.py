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
        self.probe_paths = []
        self.selections = []
        self.nodes = {"subscription": [("old", "Vless"), ("dead", "Vless"),
                                       ("good", "Vless"), ("DIRECT", "Direct")]}
        self.offline_providers = set()

        async def controller(request):
            path = request.url.path
            if path == "/version":
                return httpx.Response(200, json={"version": "test"})
            if path.startswith("/providers/proxies/") and path.count("/") == 3:
                provider = path.rsplit("/", 1)[-1]
                if provider in self.offline_providers:
                    return httpx.Response(503)
                return httpx.Response(200, json={"proxies": [
                    {"name": name, "type": kind} for name, kind in self.nodes[provider]
                ]})
            if path == "/proxies/VERTEX-OUT":
                if request.method == "PUT":
                    import json
                    self.selected = json.loads(request.content)["name"]
                    self.selections.append(self.selected)
                    return httpx.Response(204)
                return httpx.Response(200, json={"now": self.selected,
                    "all": [name for entries in self.nodes.values() for name, _ in entries]})
            if path.startswith("/providers/proxies/") and path.endswith("/healthcheck"):
                provider = path.split("/")[-3]
                name = path.split("/")[-2]
                self.assertIn(name, [entry[0] for entry in self.nodes[provider]])
                self.probes.append(name)
                self.probe_paths.append((provider, name))
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

    def add_secondary(self):
        self.pool.providers.append("subscription-2")
        self.nodes["subscription-2"] = [("new", "Hysteria2"), ("new-dead", "Tuic")]
        self.dead.add("new-dead")

    async def test_selecting_secondary_probes_the_correct_provider(self):
        self.add_secondary()
        await self.pool.start()
        await self.pool.rotate(self.pool.generation, "subscription-2")
        self.assertEqual(self.selected, "new")
        self.assertEqual(self.pool.node_providers["new"], "subscription-2")
        self.assertEqual(self.probe_paths[-1], ("subscription-2", "new"))

    async def test_unavailable_primary_does_not_block_secondary(self):
        self.add_secondary()
        self.offline_providers.add("subscription")
        await self.pool.start()
        self.assertEqual(self.selected, "new")
        self.assertEqual(self.probe_paths, [("subscription-2", "new")])

    async def test_unavailable_secondary_does_not_block_primary(self):
        self.add_secondary()
        self.offline_providers.add("subscription-2")
        await self.pool.start()
        self.assertEqual(self.selected, "old")

    async def test_duplicate_names_are_never_probed_or_selected(self):
        self.add_secondary()
        self.nodes["subscription-2"].insert(0, ("old", "Vless"))
        await self.pool.start()
        self.assertEqual(self.selected, "good")
        self.assertNotIn("old", self.probes)
        self.assertNotIn("old", self.pool.node_providers)

    async def test_check_reports_secondary_health_without_changing_selection(self):
        self.add_secondary()
        await self.pool.start()
        generation = self.pool.generation
        report = await self.pool.check("subscription-2")
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["healthy"], 1)
        self.assertEqual(report["providers"]["subscription-2"]["unhealthy"], 1)
        self.assertNotIn("subscription", report["providers"])
        self.assertEqual(self.selected, "old")
        self.assertEqual(self.pool.generation, generation)

    async def test_unknown_provider_fails_before_cooling_current_node(self):
        await self.pool.start()
        with self.assertRaises(ValueError):
            await self.pool.rotate(self.pool.generation, "unknown")
        self.assertNotIn("old", self.pool.cooldowns)
        self.assertEqual(self.selected, "old")

    async def test_health_check_does_not_clear_upstream_failure_cooldown(self):
        await self.pool.start()
        expiry = time.monotonic() + self.pool.cooldown
        self.pool.cooldowns["old"] = expiry
        report = await self.pool.check()
        self.assertEqual(report["healthy"], 2)
        self.assertEqual(self.pool.cooldowns["old"], expiry)

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
