import unittest

from pydantic import ValidationError

from app.schema import MCPConfig, ServerConfig
from app.security import SECRET_MASK, mask_secrets, validate_package_spec


class SchemaAndSecurityTests(unittest.TestCase):
    def test_server_rejects_non_local_hosts(self):
        with self.assertRaises(ValidationError):
            ServerConfig(host="example.com", port=8101)

    def test_server_rejects_privileged_ports(self):
        with self.assertRaises(ValidationError):
            ServerConfig(port=80)

    def test_config_rejects_unsafe_ids(self):
        with self.assertRaises(ValidationError):
            MCPConfig.model_validate({
                "id": "../../escape",
                "name": "Unsafe",
                "server": {"port": 8101},
                "tool_source": {"path": "tools/example.json"},
            })

    def test_secret_values_are_masked(self):
        values = {
            "api_key": "secret",
            "password": "secret",
            "base_url": "https://example.com",
            "enabled": True,
        }
        masked = mask_secrets(values)
        self.assertEqual(SECRET_MASK, masked["api_key"])
        self.assertEqual(SECRET_MASK, masked["password"])
        self.assertEqual(values["base_url"], masked["base_url"])
        self.assertTrue(masked["enabled"])

    def test_package_specs_reject_shell_metacharacters(self):
        self.assertTrue(validate_package_spec("httpx>=0.27"))
        self.assertFalse(validate_package_spec("httpx; rm -rf /"))
        self.assertFalse(validate_package_spec("$(malicious)"))


if __name__ == "__main__":
    unittest.main()
