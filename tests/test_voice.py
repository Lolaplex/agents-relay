"""Voice-in (STT) and voice-out (TTS) telegram handling."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay import telegram_adapter as ta
from agents_relay import voice


def _config(tmp: str, **over) -> RelayConfig:
    base = dict(
        loop_cmd=("echo",),
        loop_provider="echo",
        relay_secret="s",
        telegram_bot_token="tok",
        telegram_allowed_chat_ids=(7,),
        relay_host="127.0.0.1",
        relay_port=8787,
        telegram_poll_timeout=1,
        state_dir=str(Path(tmp) / "relay"),
        groq_api_key="",
    )
    base.update(over)
    return RelayConfig(**base)


def _voice_update(file_id: str = "fid") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "chat": {"id": 7},
            "from": {"id": 7},
            "voice": {"file_id": file_id, "mime_type": "audio/ogg"},
        },
    }


class VoiceInTests(unittest.TestCase):
    def test_voice_no_key_shows_notice_no_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            saved = Path(tmp) / "v.oga"
            saved.write_bytes(b"oga")
            sent: list[str] = []
            with (
                patch.object(ta, "_download_telegram_file", return_value=saved),
                patch.object(ta, "send_message", side_effect=lambda t, c, x, **kw: sent.append(x) or {"ok": True}),
            ):
                ta.process_update(_voice_update(), config=cfg)
        self.assertTrue(any("speech-to-text" in s or "GROQ_API_KEY" in s for s in sent))

    def test_voice_transcribed_feeds_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, groq_api_key="gk")
            saved = Path(tmp) / "v.oga"
            saved.write_bytes(b"oga")
            seen: dict = {}

            def fake_turn(**kw):
                seen.update(kw)
                return ta.LoopTurnResult(reply="ok", session="s", user_id="u", alias="a", returncode=0, stderr="")

            with (
                patch.object(ta, "_download_telegram_file", return_value=saved),
                patch.object(ta, "send_message", return_value={"result": {"message_id": 9}}),
                patch.object(ta, "edit_message"),
                patch.object(ta, "deliver_html"),
                patch("agents_relay.voice.transcribe_audio", return_value="hello world"),
            ):
                ta.process_update(_voice_update(), config=cfg, on_turn=fake_turn)
        self.assertEqual(seen.get("message"), "hello world")



class VoiceApiTests(unittest.TestCase):
    def test_transcribe_builds_multipart(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "v.oga"
            f.write_bytes(b"OGABYTES")
            captured: dict = {}

            class FakeResp:
                def read(self):
                    return json.dumps({"text": "hi"}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def fake_open(req, timeout=0):
                captured["url"] = req.full_url
                captured["body"] = req.data
                captured["auth"] = req.headers.get("Authorization")
                return FakeResp()

            with patch("urllib.request.urlopen", side_effect=fake_open):
                out = voice.transcribe_audio(f, "gk")
        self.assertEqual(out, "hi")
        self.assertIn("audio/transcriptions", captured["url"])
        self.assertEqual(captured["auth"], "Bearer gk")
        self.assertIn(b"OGABYTES", captured["body"])
        self.assertIn(b"whisper-large-v3-turbo", captured["body"])



if __name__ == "__main__":
    unittest.main()
