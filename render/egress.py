"""Authenticated egress diagnostics for the existing console API."""
import asyncio

import httpx

import config
from proxy_pool import proxy_pool


async def trace(proxy=None):
    try:
        async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=15) as client:
            response = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
            response.raise_for_status()
        values = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
        return {key: values.get(key) for key in ("ip", "loc")}
    except (httpx.HTTPError, ValueError):
        return {"ip": None, "loc": None}


async def status():
    if not proxy_pool.enabled:
        return {"enabled": False}
    direct, proxied = await asyncio.gather(trace(), trace(config.PROXY_URL))
    async with proxy_pool._client() as client:
        response = await client.get(f"/providers/proxies/{proxy_pool.provider}")
        response.raise_for_status()
        count = len(response.json().get("proxies", []))
    return {"enabled": True, "node": proxy_pool.current, "generation": proxy_pool.generation,
            "node_count": count, "direct_ip": direct["ip"], "exit_ip": proxied["ip"], "exit_country": proxied["loc"]}


async def rotate():
    await proxy_pool.rotate(proxy_pool.generation)
    return await status()
