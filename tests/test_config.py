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


class TestSymmetricAuth(unittest.TestCase):
    def setUp(self):
        self._keys = (
            "RELAY_SECRET", "RELAY_WEB_TOKEN", "GATEWAY_SECRET",
            "RELAY_NO_AUTH", "AGENTS_RELAY_ALLOW_ANYONE",
            "KLANKER_TELEGRAM_OPEN", "LOOP_PROVIDER", "AGENTS_KEYRING",
        )
        self._saved = {k: os.environ.get(k) for k in self._keys}
        for k in self._keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_web_token_only_authorizes_bearer(self):
        from agents_relay.config import RelayConfig

        os.environ["RELAY_WEB_TOKEN"] = "tok123"
        cfg = RelayConfig.from_env()
        self.assertEqual(cfg.web_token, "tok123")
        self.assertEqual(cfg.relay_secret, "tok123")

    def test_relay_secret_only_enables_web(self):
        from agents_relay.config import RelayConfig

        os.environ["RELAY_SECRET"] = "tok123"
        cfg = RelayConfig.from_env()
        self.assertEqual(cfg.relay_secret, "tok123")
        self.assertEqual(cfg.web_token, "tok123")

    def test_both_set_keep_own_values(self):
        from agents_relay.config import RelayConfig

        os.environ["RELAY_SECRET"] = "rs"
        os.environ["RELAY_WEB_TOKEN"] = "wt"
        cfg = RelayConfig.from_env()
        self.assertEqual(cfg.relay_secret, "rs")
        self.assertEqual(cfg.web_token, "wt")

    def test_no_auth_keeps_empty_unless_explicit(self):
        from agents_relay.config import RelayConfig

        os.environ["RELAY_NO_AUTH"] = "1"
        cfg = RelayConfig.from_env()
        self.assertEqual(cfg.relay_secret, "")
        self.assertEqual(cfg.web_token, "")
        os.environ["RELAY_SECRET"] = "explicit"
        cfg = RelayConfig.from_env()
        self.assertEqual(cfg.relay_secret, "explicit")
        self.assertEqual(cfg.web_token, "")

    def test_loop_provider_default_openai(self):
        from agents_relay.config import RelayConfig

        self.assertEqual(RelayConfig.from_env().loop_provider, "openai.default")
        os.environ["LOOP_PROVIDER"] = "echo"
        self.assertEqual(RelayConfig.from_env().loop_provider, "echo")

    def test_klanker_telegram_open_deprecated(self):
        import warnings
        from agents_relay.config import RelayConfig

        os.environ["KLANKER_TELEGRAM_OPEN"] = "1"
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            cfg = RelayConfig.from_env()
        self.assertTrue(cfg.allow_anyone)
        self.assertTrue(
            any(w.category is DeprecationWarning for w in seen))

    def test_allow_anyone_canonical(self):
        from agents_relay.config import RelayConfig

        os.environ["AGENTS_RELAY_ALLOW_ANYONE"] = "1"
        self.assertTrue(RelayConfig.from_env().allow_anyone)
