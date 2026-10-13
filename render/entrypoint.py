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


def proxy_config(subscription_url, secret):
    return {
        "mixed-port": 7890, "allow-lan": False, "bind-address": "127.0.0.1",
        "mode": "rule", "log-level": "silent", "ipv6": False,
        "external-controller": "127.0.0.1:9090", "secret": secret,
        "profile": {"store-selected": True},
        "tun": {"enable": False}, "dns": {"enable": False},
        "proxy-providers": {"subscription": {
            "type": "http", "url": subscription_url, "path": "./providers/subscription.yaml",
            "interval": 3600, "health-check": {"enable": False},
        }},
        "proxy-groups": [{"name": "VERTEX-OUT", "type": "select", "use": ["subscription"]}],
        "rules": ["MATCH,VERTEX-OUT"],
    }


def prepare_proxy(url):
    if not url.startswith("https://"):
        raise ValueError("Proxy subscription must use HTTPS")
    runtime = Path(os.environ.get("STATE_DIR", "/app/data")) / "proxy"
    providers = runtime / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "clash.meta"})
    with urllib.request.urlopen(request, timeout=45) as response:
        subscription = response.read(16 * 1024 * 1024 + 1)
    if len(subscription) > 16 * 1024 * 1024 or b"proxies:" not in subscription and b'"proxies"' not in subscription:
        raise ValueError("Subscription must contain a Clash proxies list, at most 16 MiB")
    (providers / "subscription.yaml").write_bytes(subscription)
    secret = secrets.token_hex(32)
    config_path = runtime / "config.yaml"
    config_path.write_text(json.dumps(proxy_config(url, secret), ensure_ascii=False), encoding="utf-8")
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
                       PROXY_CONTROLLER_SECRET=secret)
    environment.setdefault("PROXY_ROTATION_TIMEOUT", "15")
    environment.setdefault("PROXY_TEST_TIMEOUT_MS", "3000")
    return runtime, environment


def main():
    os.umask(0o077)
    command = [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "7860")]
    url = os.environ.get("PROXY_SUBSCRIPTION_URL", "").strip()
    if not url:
        os.execv(sys.executable, command)
    runtime, environment = prepare_proxy(url)
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
