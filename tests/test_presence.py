import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self._env = mock.patch.dict(os.environ, {"AGENTS_PRESENCE_DIR": self.dir})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _write(self, name, doc):
        (Path(self.dir) / name).write_text(json.dumps(doc), encoding="utf-8")

    def _signed_doc(self, did, exp):
        # Signature content is irrelevant — verification is mocked at the
        # agents_keys boundary (its own test suite covers real Ed25519).
        return {"type": "road-declaration/1", "id": did,
                "roads": [{"kind": "tailscale", "addr": "pc.tailnet.ts.net", "priority": 10}],
                "iat": int(time.time()), "exp": exp, "sig": "AAAA"}

    def test_list_offers_valid_expired_forged_broken(self):
        from agents_relay import presence

        self._write("good.road.json", self._signed_doc("did:key:zA", int(time.time()) + 3600))
        self._write("stale.road.json", self._signed_doc("did:key:zA", int(time.time()) - 10))
        self._write("forged.road.json", self._signed_doc("did:key:zB", int(time.time()) + 3600))
        (Path(self.dir) / "broken.json").write_text("{not json", encoding="utf-8")

        def fake_keys(args):
            if args[0] == "verify" and args[1] == "did:key:zB":
                raise ValueError("signature does not verify")
            return "valid"

        with mock.patch.object(presence, "_agents_keys", side_effect=fake_keys):
            rows = presence.list_offers()

        self.assertEqual(len(rows), 4)
        good = next(r for r in rows if r["label"] == "good")
        self.assertTrue(good["valid"], good["reason"])
        self.assertEqual(good["roads"][0]["endpoint"], "https://pc.tailnet.ts.net")

        stale = next(r for r in rows if r["label"] == "stale")
        self.assertFalse(stale["valid"])
        self.assertIn("expired", stale["reason"])

        forged = next(r for r in rows if r["label"] == "forged")
        self.assertFalse(forged["valid"])
        self.assertIn("signature", forged["reason"])
        self.assertEqual(forged["did"], "did:key:zB")

        broken = next(r for r in rows if r["label"] == "broken")
        self.assertFalse(broken["valid"])
        self.assertIn("not json", broken["reason"])

    def test_missing_registry_is_empty(self):
        from agents_relay import presence
        with mock.patch.dict(os.environ, {"AGENTS_PRESENCE_DIR": str(Path(self.dir) / "nope")}):
            self.assertEqual(presence.list_offers(), [])

    def test_publish_signs_self_verifies_writes(self):
        from agents_relay import presence

        calls = []

        def fake_keys(args):
            calls.append(args[0])
            if args[0] == "did":
                return "did:key:zPub"
            if args[0] == "sign":
                return "ab" * 64
            return "valid"

        with mock.patch.object(presence, "_agents_keys", side_effect=fake_keys):
            offer, err = presence.publish_offer(
                "mybox", "sigma", [{"kind": "tailscale", "addr": "10.0.0.1:8787"}])

        self.assertIsNotNone(offer, err)
        self.assertEqual(calls[:2], ["did", "sign"])
        written = Path(self.dir) / "sigma.road.json"
        self.assertTrue(written.is_file())
        doc = json.loads(written.read_text())
        self.assertEqual(doc["id"], "did:key:zPub")
        self.assertTrue(doc["sig"])
        # base64url, no padding
        base64.urlsafe_b64decode(doc["sig"] + "=" * (-len(doc["sig"]) % 4))

    def test_publish_refuses_unverifiable(self):
        from agents_relay import presence

        def fake_keys(args):
            if args[0] == "did":
                return "did:key:zPub"
            if args[0] == "sign":
                return "ab" * 64
            raise ValueError("signature does not verify")

        with mock.patch.object(presence, "_agents_keys", side_effect=fake_keys):
            offer, err = presence.publish_offer(
                "mybox", "sigma", [{"kind": "tailscale", "addr": "x"}])
        self.assertIsNone(offer)
        self.assertIn("self-check", err)
        self.assertFalse((Path(self.dir) / "sigma.road.json").exists())

    def test_publish_rejects_bad_input(self):
        from agents_relay import presence
        offer, err = presence.publish_offer("k", "BAD LABEL!", [{"kind": "t", "addr": "a"}])
        self.assertIsNone(offer)
        offer, err = presence.publish_offer("k", "ok", [])
        self.assertIsNone(offer)
        self.assertIn("no roads", err)


if __name__ == "__main__":
    unittest.main()
