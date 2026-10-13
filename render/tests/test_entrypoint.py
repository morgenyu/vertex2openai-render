import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import render_entrypoint as entrypoint


class SubscriptionTests(unittest.TestCase):
    def test_primary_is_retained_and_duplicate_urls_are_removed(self):
        with patch.dict(os.environ, {"PROXY_SUBSCRIPTION_URL": "https://primary.example/sub",
                "PROXY_SUBSCRIPTION_URLS": json.dumps(["https://secondary.example/sub",
                                                     "https://primary.example/sub"])}, clear=True):
            self.assertEqual(entrypoint.subscription_urls(),
                             ["https://primary.example/sub", "https://secondary.example/sub"])

    def test_malformed_additional_subscription_list_is_rejected(self):
        for value in ('"https://secondary.example/sub"', '[123]', '{"url": "private"}'):
            with patch.dict(os.environ, {"PROXY_SUBSCRIPTION_URLS": value}, clear=True):
                with self.assertRaises(ValueError):
                    entrypoint.subscription_urls()

    def test_two_providers_have_distinct_prefixes_and_shared_selector(self):
        config = entrypoint.proxy_config(["https://primary.example/sub", "https://secondary.example/sub"], "secret")
        providers = config["proxy-providers"]
        self.assertEqual(config["proxy-groups"][0]["use"], ["subscription", "subscription-2"])
        self.assertNotEqual(providers["subscription"]["override"], providers["subscription-2"]["override"])
        self.assertEqual(providers["subscription-2"]["header"]["User-Agent"], ["clash.meta"])

    def test_single_provider_keeps_legacy_names(self):
        provider = entrypoint.proxy_config("https://primary.example/sub", "secret")["proxy-providers"]["subscription"]
        self.assertNotIn("override", provider)

    def test_one_failed_download_keeps_the_successful_provider(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"STATE_DIR": directory}, clear=True), \
                patch.object(entrypoint.urllib.request, "urlopen",
                             side_effect=[OSError("private-url"), io.BytesIO(b"proxies: []\n")]), \
                patch.object(entrypoint.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                patch("builtins.print") as output:
            runtime, environment = entrypoint.prepare_proxy(["https://primary.example/sub", "https://secondary.example/sub"])
            config = json.loads((runtime / "config.yaml").read_text())
            self.assertEqual(environment["PROXY_PROVIDERS"], "subscription-2")
            self.assertEqual(config["proxy-groups"][0]["use"], ["subscription-2"])
            self.assertTrue((Path(runtime) / "providers/subscription-2.yaml").exists())
            self.assertNotIn("private-url", str(output.call_args_list))

    def test_all_failed_downloads_fail_without_direct_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"STATE_DIR": directory}, clear=True), \
                patch.object(entrypoint.urllib.request, "urlopen", side_effect=OSError()), \
                patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                entrypoint.prepare_proxy(["https://primary.example/sub", "https://secondary.example/sub"])


if __name__ == "__main__":
    unittest.main()
