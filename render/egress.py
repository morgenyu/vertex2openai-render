"""Authenticated egress diagnostics for the existing console API."""
import asyncio

import httpx
from fastapi import HTTPException

import config
from proxy_pool import proxy_pool, ProxyPoolUnavailable


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
        _, inventory = await proxy_pool._inventory(client)
    providers = {key: {"node_count": len(names or []), "available": names is not None}
                 for key, names in inventory.items()}
    return {"enabled": True, "node": proxy_pool.current, "generation": proxy_pool.generation,
            "provider": proxy_pool.node_providers.get(proxy_pool.current), "providers": providers,
            "node_count": sum(item["node_count"] for item in providers.values()),
            "direct_ip": direct["ip"], "exit_ip": proxied["ip"], "exit_country": proxied["loc"]}


async def rotate(provider=None):
    try:
        await proxy_pool.rotate(proxy_pool.generation, provider)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown proxy provider") from None
    except ProxyPoolUnavailable:
        raise HTTPException(status_code=503, detail="No healthy replacement proxy is available") from None
    return await status()


async def check(provider=None):
    if not proxy_pool.enabled:
        return {"enabled": False}
    try:
        return await proxy_pool.check(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown proxy provider") from None
