import os
import unittest



class TestEnvAliases(unittest.TestCase):
    def setUp(self):
        for k in ("AGENTS_RELAY_DIR", "AGENTS_RELAY_STATE", "RELAY_SECRET",
                  "GATEWAY_SECRET", "RELAY_HOST", "GATEWAY_HOST",
                  "RELAY_PORT", "GATEWAY_PORT", "AGENTS_RELAY_WEB_DIR",
                  "RELAY_STATIC_DIR"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.setUp()

    def test_new_state_dir_wins(self):
        from agents_relay.config import RelayConfig
        os.environ["AGENTS_RELAY_DIR"] = "/new"
        os.environ["AGENTS_RELAY_STATE"] = "/old"
        self.assertEqual(RelayConfig.from_env().state_dir, "/new")

    def test_old_state_dir_warns(self):
        import warnings
        from agents_relay.config import RelayConfig
        os.environ["AGENTS_RELAY_STATE"] = "/old"
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            cfg = RelayConfig.from_env()
        self.assertEqual(cfg.state_dir, "/old")
        self.assertTrue(any("AGENTS_RELAY_STATE is deprecated" in str(w.message) for w in seen))

    def test_gateway_secret_alias_warns(self):
        import warnings
        from agents_relay.config import RelayConfig
        os.environ["GATEWAY_SECRET"] = "x"
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            cfg = RelayConfig.from_env()
        self.assertEqual(cfg.relay_secret, "x")
        self.assertTrue(any("GATEWAY_SECRET is deprecated" in str(w.message) for w in seen))

    def test_web_dir_new_wins(self):
        from agents_relay.config import RelayConfig
        os.environ["AGENTS_RELAY_WEB_DIR"] = "/webnew"
        os.environ["RELAY_STATIC_DIR"] = "/webold"
        self.assertEqual(RelayConfig.from_env().static_dir, "/webnew")
