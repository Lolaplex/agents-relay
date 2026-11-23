"""Hub session run-lock leases: TTL, fencing, contention, renew/release."""
import unittest

from agents_relay.leases import LeaseBook


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def mk():
    clk = FakeClock()
    return clk, LeaseBook(clock=clk)


class TestAcquire(unittest.TestCase):
    def test_grant_then_deny_other_holder(self):
        clk, book = mk()
        r1 = book.acquire("w", "s1", "dev-a", "run-1")
        self.assertTrue(r1["granted"])
        fence1 = r1["fence"]
        r2 = book.acquire("w", "s1", "dev-b", "run-2")
        self.assertFalse(r2["granted"])
        self.assertEqual(r2["holder"], "dev-a")

    def test_same_holder_renews_implicitly(self):
        clk, book = mk()
        book.acquire("w", "s", "a", "r1")
        r = book.acquire("w", "s", "a", "r1")  # same run re-acquires
        self.assertTrue(r["granted"])

    def test_expiry_frees_slot(self):
        clk, book = mk()
        book.acquire("w", "s", "a")
        clk.advance(46.0)
        r = book.acquire("w", "s", "b")
        self.assertTrue(r["granted"])

    def test_fence_increments(self):
        clk, book = mk()
        f1 = book.acquire("w", "s", "a")["fence"]
        f2 = book.acquire("w", "s2", "a")["fence"]
        self.assertGreater(f2, f1)


class TestRenewRelease(unittest.TestCase):
    def test_renew_extends(self):
        clk, book = mk()
        f = book.acquire("w", "s", "a")["fence"]
        clk.advance(30.0)
        r = book.renew("w", "s", "a", f)
        self.assertTrue(r["granted"])
        clk.advance(30.0)  # would have expired without renew
        r2 = book.acquire("w", "s", "b")
        self.assertFalse(r2["granted"])

    def test_renew_wrong_holder_or_fence_fails(self):
        clk, book = mk()
        f = book.acquire("w", "s", "a")["fence"]
        self.assertFalse(book.renew("w", "s", "b", f)["granted"])
        self.assertFalse(book.renew("w", "s", "a", f + 1)["granted"])

    def test_release_then_reacquire(self):
        clk, book = mk()
        f = book.acquire("w", "s", "a")["fence"]
        book.release("w", "s", "a", f)
        r = book.acquire("w", "s", "b")
        self.assertTrue(r["granted"])

    def test_stale_release_is_noop(self):
        clk, book = mk()
        f1 = book.acquire("w", "s", "a")["fence"]
        clk.advance(46.0)
        book.acquire("w", "s", "b")
        book.release("w", "s", "a", f1)  # stale holder must not evict b
        r = book.acquire("w", "s", "c")
        self.assertFalse(r["granted"])  # b still holds it


class TestSnapshot(unittest.TestCase):
    def test_snapshot_skips_expired(self):
        clk, book = mk()
        book.acquire("w", "live", "a")
        book.acquire("w", "dead", "a")
        clk.advance(46.0)
        book.acquire("w", "live2", "a")
        rows = book.snapshot()
        sessions = sorted(r["session"] for r in rows)
        self.assertEqual(sessions, ["live2"])
        # note: "live" + "dead" both expired at t=46


class TestIdempotentReacquire(unittest.TestCase):
    def test_same_run_keeps_fence_and_extends(self):
        clk, book = mk()
        f1 = book.acquire("w", "s", "a", "r1")["fence"]
        clk.advance(40.0)
        r = book.acquire("w", "s", "a", "r1")
        self.assertTrue(r["granted"])
        self.assertEqual(r["fence"], f1)
        clk.advance(40.0)  # 80s after first grant, 40s after re-acquire
        self.assertFalse(book.acquire("w", "s", "b")["granted"])

    def test_same_holder_other_run_denied_while_live(self):
        clk, book = mk()
        book.acquire("w", "s", "a", "r1")
        self.assertFalse(book.acquire("w", "s", "a", "r2")["granted"])


class TestFencePersistence(unittest.TestCase):
    def test_fence_monotonic_across_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "hub_leases.json"
            b1 = LeaseBook(clock=FakeClock(), state_path=state, wall=lambda: 0.0)
            f1 = b1.acquire("w", "s", "a")["fence"]
            f2 = b1.acquire("w", "s2", "a")["fence"]
            self.assertGreater(f2, f1)
            # restart: wall clock even went backwards — persisted mark wins
            b2 = LeaseBook(clock=FakeClock(), state_path=state, wall=lambda: 0.0)
            f3 = b2.acquire("w", "s", "b")["fence"]
            self.assertGreater(f3, f2)

    def test_missing_state_seeds_from_wall_clock(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "nested" / "hub_leases.json"
            b = LeaseBook(clock=FakeClock(), state_path=state,
                          wall=lambda: 1_700_000_000.0)
            f = b.acquire("w", "s", "a")["fence"]
            self.assertGreater(f, 1_700_000_000_000)
            self.assertTrue(state.is_file())

    def test_corrupt_state_falls_back(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "hub_leases.json"
            state.write_text("{not json", encoding="utf-8")
            b = LeaseBook(clock=FakeClock(), state_path=state, wall=lambda: 5.0)
            self.assertEqual(b.acquire("w", "s", "a")["fence"], 5001)


if __name__ == "__main__":
    unittest.main()
