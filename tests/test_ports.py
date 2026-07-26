import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import config_store, shared_proxy


class PortSafetyTests(unittest.TestCase):
    def test_shared_port_is_not_reported_as_free(self):
        with patch("app.settings_store.load_settings", return_value={"shared_port": 8456}):
            self.assertFalse(config_store.is_port_free(8456))

    def test_allocator_skips_shared_port(self):
        with (
            patch("app.settings_store.load_settings", return_value={"shared_port": 8456}),
            patch("app.config_store.load_all_configs", return_value={}),
            patch("app.config_store._os_port_free", return_value=True),
        ):
            self.assertEqual(8457, config_store.find_free_port(8456))

    def test_proxy_rejects_ports_used_by_stopped_or_running_instances(self):
        configs = {
            "alpha": SimpleNamespace(id="alpha", server=SimpleNamespace(port=8456)),
            "beta": SimpleNamespace(id="beta", server=SimpleNamespace(port=8456)),
        }
        with patch("app.shared_proxy.load_all_configs", return_value=configs):
            ok, error = asyncio.run(shared_proxy.start_proxy(8456, "127.0.0.1"))
        self.assertFalse(ok)
        self.assertIn("alpha, beta", error)
        self.assertFalse(shared_proxy.proxy_running())


if __name__ == "__main__":
    unittest.main()
