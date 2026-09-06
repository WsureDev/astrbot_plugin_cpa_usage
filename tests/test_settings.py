import unittest

from cpa_usage.settings import as_bool, client_from_config, find_provider_config


class SettingsTests(unittest.TestCase):
    def test_provider_match_is_exact_and_skips_disabled(self):
        providers = [
            {"provider_id": "main", "enabled": False},
            {"provider_id": "main-backup", "enabled": True},
            {"provider_id": "main", "enabled": True, "base_url": "http://cpa", "token": "key"},
        ]
        self.assertIs(find_provider_config(providers, "main"), providers[2])
        self.assertIsNone(find_provider_config(providers, "MAIN"))

    def test_provider_match_treats_string_false_as_disabled(self):
        providers = [
            {"provider_id": "main", "enabled": "false"},
            {"provider_id": "main", "enabled": "true"},
        ]
        self.assertIs(find_provider_config(providers, "main"), providers[1])

    def test_client_config_parses_string_boolean(self):
        client = client_from_config(
            {"base_url": "http://cpa/", "token": "key", "tls_verify": "false"},
            default_tls_verify=True,
        )
        self.assertEqual(client.base_url, "http://cpa")
        self.assertFalse(client.tls_verify)
        self.assertFalse(as_bool("off"))

    def test_client_config_requires_url_and_token(self):
        with self.assertRaisesRegex(ValueError, "base_url"):
            client_from_config({"token": "key"})
        with self.assertRaisesRegex(ValueError, "token"):
            client_from_config({"base_url": "http://cpa"})
