"""Sticky, per-project Mihomo selector used only at startup and before retries."""

import asyncio
import logging
import os
import random
import time
from urllib.parse import quote

import httpx


LOG = logging.getLogger("uvicorn.error")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class ProxyPoolUnavailable(RuntimeError):
    code = 503


def is_proxy_retryable(error) -> bool:
    if isinstance(error, ProxyPoolUnavailable):
        return False
    if isinstance(error, httpx.TransportError):
        return True
    for value in (getattr(error, "status_code", None), getattr(error, "code", None),
                  getattr(getattr(error, "response", None), "status_code", None)):
        if isinstance(value, int):
            return value in RETRY_STATUSES
    text = str(error).lower()
    return any(marker in text for marker in (
        "429", "500", "502", "503", "504", "408", "resource exhausted",
        "resource_exhausted", "rate limit", "too many requests", "quota", "overloaded",
        "try again later", "timeout", "timed out", "connection reset", "connection refused",
        "connection closed", "connection aborted", "connecterror", "readerror",
        "server disconnected", "proxyerror", "name resolution", "all connection attempts failed",
    ))


class ProxyPool:
    def __init__(self):
        self.controller = os.environ.get("PROXY_CONTROLLER_URL", "").rstrip("/")
        self.secret = os.environ.get("PROXY_CONTROLLER_SECRET", "")
        self.group = os.environ.get("PROXY_GROUP", "VERTEX-OUT")
        self.provider = os.environ.get("PROXY_PROVIDER", "subscription")
        self.test_url = os.environ.get("PROXY_TEST_URL", "https://www.google.com/generate_204")
        self.probe_ms = int(os.environ.get("PROXY_TEST_TIMEOUT_MS", "4000"))
        self.rotation_timeout = float(os.environ.get("PROXY_ROTATION_TIMEOUT", "30"))
        self.cooldown = float(os.environ.get("PROXY_NODE_COOLDOWN", "300"))
        self.batch_size = int(os.environ.get("PROXY_PROBE_CONCURRENCY", "6"))
        self.generation = 0
        self.current = None
        self.cooldowns = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.controller)

    def _client(self):
        # Controller traffic must never be sent through the outbound proxy.
        return httpx.AsyncClient(
            base_url=self.controller,
            headers={"Authorization": f"Bearer {self.secret}"},
            trust_env=False,
            timeout=self.probe_ms / 1000 + 2,
        )

    async def _probe(self, client, name):
        try:
            # Provider nodes are not present in Mihomo's top-level /proxies map.
            path = f"/providers/proxies/{quote(self.provider, safe='')}/{quote(name, safe='')}/healthcheck"
            response = await client.get(path, params={
                "url": self.test_url, "timeout": self.probe_ms, "expected-status": "204",
            })
            response.raise_for_status()
            delay = response.json().get("delay")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 0:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        self.cooldowns[name] = time.monotonic() + self.cooldown
        return False

    async def _select(self, client, name):
        response = await client.put(f"/proxies/{quote(self.group, safe='')}", json={"name": name})
        response.raise_for_status()
        self.current = name
        self.generation += 1
        # Do not DELETE /connections: healthy concurrent streams must keep running.
        LOG.info("[Proxy pool] Selected %s (generation %s)", name, self.generation)

    async def _choose(self, failed):
        async with self._client() as client:
            group_response = await client.get(f"/proxies/{quote(self.group, safe='')}")
            group_response.raise_for_status()
            group = group_response.json()
            response = await client.get(f"/providers/proxies/{quote(self.provider, safe='')}")
            response.raise_for_status()
            entries = response.json().get("proxies", [])
            # Only actual leaf proxies from this provider; never DIRECT/REJECT/groups.
            allowed = {"ss", "ssr", "shadowsocks", "shadowsocksr", "vmess", "vless", "trojan",
                       "hysteria", "hysteria2", "tuic", "wireguard", "socks5", "http", "ssh",
                       "mieru", "anytls", "snell"}
            available = set(group.get("all", []))
            names = list(dict.fromkeys(p["name"] for p in entries
                         if p.get("name") in available and str(p.get("type", "")).lower() in allowed))
            now = time.monotonic()
            self.cooldowns = {name: expiry for name, expiry in self.cooldowns.items() if expiry > now}
            current = group.get("now")
            if failed is None and current in names and current not in self.cooldowns:
                if await self._probe(client, current):
                    await self._select(client, current)
                    return
            candidates = [name for name in names if name != failed and name not in self.cooldowns]
            random.shuffle(candidates)
            LOG.info("[Proxy pool] Checking candidates (%d nodes, %d eligible)", len(names), len(candidates))
            for offset in range(0, len(candidates), self.batch_size):
                batch = candidates[offset:offset + self.batch_size]
                results = await asyncio.gather(*(self._probe(client, name) for name in batch))
                for name, healthy in zip(batch, results):
                    if healthy:
                        await self._select(client, name)
                        return
            raise ProxyPoolUnavailable("No healthy proxy node is available; try again after cooldown.")

    async def start(self):
        if not self.enabled:
            return
        # Docker may start the app before the controller/provider has finished loading.
        for attempt in range(15):
            try:
                async with self._client() as client:
                    response = await client.get("/version")
                    response.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 14:
                    raise ProxyPoolUnavailable("Proxy controller did not become ready.") from None
                await asyncio.sleep(1)
        async with self._lock:
            try:
                await asyncio.wait_for(self._choose(None), timeout=self.rotation_timeout)
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise ProxyPoolUnavailable("Could not initialize a healthy proxy node.") from exc

    async def rotate(self, failed_generation):
        if not self.enabled:
            return
        async with self._lock:
            # Concurrent failures on the same old node cause only one selection change.
            if failed_generation != self.generation:
                return
            failed = self.current
            if failed:
                self.cooldowns[failed] = time.monotonic() + self.cooldown
            LOG.warning("[Proxy pool] Upstream retry: cooling down %s and finding another node", failed)
            try:
                await asyncio.wait_for(self._choose(failed), timeout=self.rotation_timeout)
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise ProxyPoolUnavailable("Could not select a healthy replacement proxy node.") from exc


proxy_pool = ProxyPool()
