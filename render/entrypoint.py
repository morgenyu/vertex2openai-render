"""Run the API and its private Mihomo egress in the same Render container."""
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.request


def subscription_urls():
    primary = os.environ.get("PROXY_SUBSCRIPTION_URL", "").strip()
    extra = json.loads(os.environ.get("PROXY_SUBSCRIPTION_URLS", "[]"))
    if not isinstance(extra, list) or any(not isinstance(url, str) for url in extra):
        raise ValueError("Additional subscriptions must be a JSON list of URLs")
    return list(dict.fromkeys(url.strip() for url in [primary, *extra] if url.strip()))


def proxy_config(subscription_url, secret):
    urls = [subscription_url] if isinstance(subscription_url, str) else subscription_url
    providers = {}
    for index, url in enumerate(urls, 1):
        name = "subscription" if index == 1 else f"subscription-{index}"
        providers[name] = {
            "type": "http", "url": url, "path": f"./providers/{name}.yaml",
            "interval": 3600, "health-check": {"enable": False},
            "header": {"User-Agent": ["clash.meta"]},
        }
        if len(urls) > 1:
            providers[name]["override"] = {"additional-prefix": f"[{name}] "}
    return {
        "mixed-port": 7890, "allow-lan": False, "bind-address": "127.0.0.1",
        "mode": "rule", "log-level": "silent", "ipv6": False,
        "external-controller": "127.0.0.1:9090", "secret": secret,
        "profile": {"store-selected": True},
        "tun": {"enable": False}, "dns": {"enable": False},
        "proxy-providers": providers,
        "proxy-groups": [{"name": "VERTEX-OUT", "type": "select", "use": list(providers)}],
        "rules": ["MATCH,VERTEX-OUT"],
    }


def prepare_proxy(url):
    urls = [url] if isinstance(url, str) else list(url)
    if not urls or any(not value.startswith("https://") for value in urls):
        raise ValueError("Proxy subscription must use HTTPS")
    runtime = Path(os.environ.get("STATE_DIR", "/app/data")) / "proxy"
    providers = runtime / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    config = proxy_config(urls, secret)
    unavailable = []
    for name, provider in config["proxy-providers"].items():
        try:
            request = urllib.request.Request(provider["url"], headers={"User-Agent": "clash.meta"})
            with urllib.request.urlopen(request, timeout=45) as response:
                subscription = response.read(16 * 1024 * 1024 + 1)
            if len(subscription) > 16 * 1024 * 1024 or b"proxies:" not in subscription and b'"proxies"' not in subscription:
                raise ValueError("Subscription must contain a Clash proxies list, at most 16 MiB")
            (providers / f"{name}.yaml").write_bytes(subscription)
        except (OSError, ValueError) as error:
            unavailable.append(name)
            print(f"Proxy provider {name} unavailable ({type(error).__name__}).", flush=True)
    for name in unavailable:
        del config["proxy-providers"][name]
    active = list(config["proxy-providers"])
    if not active:
        raise RuntimeError("No subscription could be loaded")
    config["proxy-groups"][0]["use"] = active
    config_path = runtime / "config.yaml"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    validation = subprocess.run(["mihomo", "-t", "-d", str(runtime)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if validation.returncode:
        raise RuntimeError("Mihomo rejected the subscription configuration")
    # The controller and proxy are private loopback listeners. Never expose them
    # through Render or pass their credentials to the subscription provider.
    environment = os.environ.copy()
    environment.update(PROXY_URL="http://127.0.0.1:7890", HTTP_PROXY="http://127.0.0.1:7890",
                       HTTPS_PROXY="http://127.0.0.1:7890", ALL_PROXY="http://127.0.0.1:7890",
                       NO_PROXY="127.0.0.1,localhost", PROXY_CONTROLLER_URL="http://127.0.0.1:9090",
                       PROXY_CONTROLLER_SECRET=secret, PROXY_PROVIDERS=",".join(active))
    environment.setdefault("PROXY_ROTATION_TIMEOUT", "15")
    environment.setdefault("PROXY_TEST_TIMEOUT_MS", "3000")
    return runtime, environment


def main():
    os.umask(0o077)
    command = [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "7860")]
    urls = subscription_urls()
    if not urls:
        os.execv(sys.executable, command)
    runtime, environment = prepare_proxy(urls)
    children = []
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Mihomo errors can contain the private subscription URL. Its control API
        # provides diagnostics while its raw output stays off the public logs.
        children.append(subprocess.Popen(["mihomo", "-d", str(runtime)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        children.append(subprocess.Popen(command, env=environment))
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError("An API or egress process stopped")
            time.sleep(0.5)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception text might contain a subscription URL or node credentials.
        print(f"Render startup failed ({type(error).__name__}); check egress availability.", flush=True)
        raise SystemExit(1) from None
