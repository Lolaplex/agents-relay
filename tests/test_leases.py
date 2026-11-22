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


if __name__ == "__main__":
    unittest.main()
