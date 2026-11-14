"""Inbox copies the workdir can read, and a visible note when vision is off."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_relay.attachments import (
    IMAGE_UNAVAILABLE_PREFIX,
    MAX_ATTACHMENT_BYTES,
    MIRROR_DIRNAME,
    apply_notes,
    find_provider_manifest,
    materialize_attachments,
    prepare_turn_files,
    read_capped,
    vision_active,
    vision_notice,
)
from agents_relay.config import RelayConfig
from agents_relay.loop_client import run_loop_turn


def _cfg(tmp: Path, **overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(tmp),
    )
    base.update(overrides)
    return RelayConfig(**base)


def _can_symlink() -> bool:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to(Path(tmp) / "missing")
        return True
    except (OSError, NotImplementedError):
        return False


class _Chunk:
    def __init__(self, blob: bytes, step: int = 8) -> None:
        self._blob = blob
        self._step = step
        self._at = 0

    def read(self, _n: int = -1) -> bytes:
        if self._at >= len(self._blob):
            return b""
        piece = self._blob[self._at:self._at + self._step]
        self._at += len(piece)
        return piece


class TestReadCap(unittest.TestCase):
    def test_over_cap_returns_none(self):
        self.assertIsNone(read_capped(_Chunk(b"abcdef", step=2), limit=4))

    def test_under_cap_returns_bytes(self):
        self.assertEqual(read_capped(_Chunk(b"abcdef", step=2), limit=6), b"abcdef")


class TestMirror(unittest.TestCase):
    def test_trusted_file_lands_in_workdir_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            src = inbox / "shot.png"
            src.write_bytes(b"\x89PNG\r\n\x1a\n")
            work = root / "work"
            cfg = _cfg(root)
            prepared = prepare_turn_files(
                [str(src)], config=cfg, workdir=str(work),
            )
            self.assertEqual(len(prepared.paths), 1)
            saved = Path(prepared.paths[0])
            self.assertEqual(saved.parent.name, MIRROR_DIRNAME)
            self.assertTrue(saved.resolve().is_relative_to((work / MIRROR_DIRNAME).resolve()))
            self.assertEqual(saved.read_bytes(), src.read_bytes())
            self.assertTrue(saved.name.startswith("file_"))
            self.assertEqual(saved.suffix, ".png")
            if os.name != "nt":
                self.assertEqual(saved.stat().st_mode & 0o222, 0)
            self.assertEqual(prepared.allow_root, "")
            self.assertFalse(prepared.notes)

    def test_outside_trust_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            src = Path(other) / "a.png"
            src.write_bytes(b"png")
            prepared = prepare_turn_files(
                [str(src)], config=_cfg(Path(tmp)), workdir=str(Path(tmp) / "work"),
            )
            self.assertEqual(prepared.paths, (str(src),))
            self.assertFalse((Path(tmp) / "work" / MIRROR_DIRNAME).exists())

    def test_oversize_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "big.png"
            src.write_bytes(b"x")
            os.truncate(src, MAX_ATTACHMENT_BYTES + 1)
            prepared = prepare_turn_files(
                [str(src)], config=_cfg(root), workdir=str(root / "work"),
            )
            self.assertEqual(prepared.paths, ())
            self.assertTrue(any("20 MB" in note for note in prepared.notes))

    def test_no_workdir_allowlists_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            src = inbox / "notes.txt"
            src.write_text("hello", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("AGENTS_WORKDIR", "AGENTS_WORKSPACE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                prepared = prepare_turn_files([str(src)], config=_cfg(root))
            self.assertEqual(prepared.paths, (str(src),))
            self.assertEqual(Path(prepared.allow_root).resolve(), inbox.resolve())
            self.assertTrue(any("relay inbox" in note for note in prepared.notes))

    @unittest.skipUnless(_can_symlink(), "symlinks unavailable")
    def test_symlink_source_and_mirror_dir_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            secret = Path(outside) / "secret.png"
            secret.write_bytes(b"nope")
            link = inbox / "shot.png"
            link.symlink_to(secret)
            work = root / "work"
            work.mkdir()
            mirror = work / MIRROR_DIRNAME
            mirror.symlink_to(Path(outside))
            prepared = prepare_turn_files(
                [str(link)], config=_cfg(root), workdir=str(work),
            )
            self.assertEqual(prepared.paths, ())
            self.assertTrue(any("symlink" in note for note in prepared.notes))
            plain = inbox / "plain.png"
            plain.write_bytes(b"png")
            again = prepare_turn_files(
                [str(plain)], config=_cfg(root), workdir=str(work),
            )
            self.assertEqual(list(Path(outside).iterdir()), [secret])
            self.assertTrue(any("may not read" in note for note in again.notes))
            self.assertEqual(again.paths, (str(plain),))


class TestVision(unittest.TestCase):
    def test_manifest_capability_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "openai.default.json").write_text(
                '{"name":"openai.default","capabilities":["vision"]}\n',
                encoding="utf-8",
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_VISION", "AGENTS_RELAY_VISION", "AGENTS_PROVIDERS_DIR")
            }
            env["AGENTS_PROVIDERS_DIR"] = str(folder)
            with patch.dict(os.environ, env, clear=True):
                self.assertTrue(vision_active(_cfg(folder), "openai.default"))
                off = _cfg(folder, vision="off")
                self.assertFalse(vision_active(off, "openai.default"))
                self.assertEqual(find_provider_manifest("openai.default")["name"], "openai.default")
                self.assertIsNone(find_provider_manifest("../x"))

    def test_notice_names_the_image(self):
        note = vision_notice(["/work/.relay-inbox/file_ab.png", "/work/a.txt"], active=False)
        self.assertTrue(note.startswith(IMAGE_UNAVAILABLE_PREFIX))
        self.assertIn("file_ab.png", note)
        self.assertNotIn("a.txt", note)
        self.assertIn(".relay-inbox", note)
        self.assertEqual(vision_notice(["/work/.relay-inbox/file_ab.png"], active=True), "")
        self.assertEqual(apply_notes("pong\n" + note, [note]), "pong\n" + note)

    def test_turn_reply_mentions_unsupported_image_and_mirrors(self):
        trailer = '{"session":"s","user_id":"u","alias":"a"}'

        class FakeProc:
            returncode = 0
            stdout = f"pong\n---agents-loop-trailer---\n{trailer}\n"
            stderr = ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "inbox" / "pic.png"
            src.parent.mkdir()
            src.write_bytes(b"png-bytes")
            work = root / "work"
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_VISION", "AGENTS_RELAY_VISION", "AGENTS_PROVIDERS_DIR", "AGENTS_WORKDIR")
            }
            with patch.dict(os.environ, env, clear=True):
                with patch("agents_relay.loop_client.subprocess.run", return_value=FakeProc()) as run:
                    result = run_loop_turn(
                        channel="http",
                        user="u",
                        message="look",
                        config=_cfg(root, vision="off"),
                        attachments=[str(src)],
                        workdir=str(work),
                    )
            argv = run.call_args.args[0]
            attached = argv[argv.index("--attach") + 1]
            self.assertTrue(attached.startswith(str(work)))
            self.assertIn(MIRROR_DIRNAME, attached)
            self.assertEqual(Path(attached).read_bytes(), b"png-bytes")
            self.assertIn(IMAGE_UNAVAILABLE_PREFIX, result.reply)
            self.assertIn("pong", result.reply)
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["AGENTS_VISION"], "0")
            self.assertIn(IMAGE_UNAVAILABLE_PREFIX, argv[argv.index("--message") + 1])


class TestMaterializeNotes(unittest.TestCase):
    def test_bad_url_is_noted(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes: list[str] = []
            paths = materialize_attachments(
                [{"url": "file:///etc/passwd"}],
                Path(tmp),
                notes=notes,
            )
            self.assertEqual(paths, [])
            self.assertTrue(notes)
            self.assertIn("dropped", notes[0].lower())


if __name__ == "__main__":
    unittest.main()
