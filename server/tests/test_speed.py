"""Driving fast (2026-10-25): a block's photo waits only as long as the drive to the block's edge allows
(Server._base_wait_s, Connection.edge_at), and the server borrows some of the game's cores (Server.speed_boost)."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpupin  # noqa: E402
import earthd  # noqa: E402
import farfield  # noqa: E402


class EdgeAtTest(unittest.TestCase):
    def conn(self):
        return SimpleNamespace(block=farfield.Block(0.0, 0.0, 512))     # collidable [-512, 510]

    def test_time_to_the_pause(self):
        c = self.conn()
        at = earthd.Connection.edge_at(c, 0.0, 0.0, 50.0, 0.0)
        want = (510 - earthd.BASE_EDGE_STOP_M) / 50.0 - earthd.BASE_EDGE_LEAD_S
        self.assertAlmostEqual(at - time.monotonic(), want, delta=0.05)
        # diagonal: the nearer edge counts; west and south too
        at = earthd.Connection.edge_at(c, 300.0, 0.0, 30.0, -30.0)
        self.assertAlmostEqual(at - time.monotonic(), (510 - 15 - 300) / 30.0 - 0.5, delta=0.05)
        at = earthd.Connection.edge_at(c, 0.0, 0.0, 0.0, -40.0)
        self.assertAlmostEqual(at - time.monotonic(), (512 - 15) / 40.0 - 0.5, delta=0.05)

    def test_past_the_line_is_now(self):
        at = earthd.Connection.edge_at(self.conn(), 505.0, 0.0, 40.0, 0.0)
        self.assertAlmostEqual(at, time.monotonic(), delta=0.05)

    def test_slow_or_no_block(self):
        self.assertIsNone(earthd.Connection.edge_at(self.conn(), 0.0, 0.0, 10.0, 0.0))
        self.assertIsNone(earthd.Connection.edge_at(SimpleNamespace(block=None), 0.0, 0.0, 50.0, 0.0))


class BaseWaitTest(unittest.TestCase):
    def wait(self, fast=False, edge_in=None):
        s = SimpleNamespace(fast=fast, edge_at=None if edge_in is None else time.monotonic() + edge_in)
        return earthd.Server._base_wait_s(s)

    def test_standing_waits_for_the_photo(self):
        self.assertIsNone(self.wait())

    def test_waits_until_the_margin(self):
        self.assertAlmostEqual(self.wait(edge_in=6.0), 6.0 - earthd.BASE_MARGIN_S, delta=0.05)
        self.assertEqual(self.wait(edge_in=0.5), earthd.BASE_FAST_WAIT_S)
        self.assertEqual(self.wait(edge_in=-3.0), earthd.BASE_FAST_WAIT_S)     # paused at the edge already

    def test_very_fast_hardly_waits(self):
        self.assertEqual(self.wait(fast=True, edge_in=20.0), earthd.BASE_FAST_WAIT_S)


class SpeedBoostTest(unittest.TestCase):
    def setUp(self):
        self.pinned = []
        self.old = cpupin.pin_tree
        cpupin.pin_tree = lambda cpus, root=None: self.pinned.append(set(cpus)) or 1
        self.s = earthd.Server.__new__(earthd.Server)
        self.s.server_cpus, self.s.speed_cpus, self.s.boost_cpus = {6, 7}, {4, 5, 6, 7}, {3, 4, 5, 6, 7}

    def tearDown(self):
        cpupin.pin_tree = self.old

    def test_speed_and_load_boost_together(self):
        s = self.s
        s.speed_boost(True, "test")
        self.assertEqual(self.pinned[-1], {4, 5, 6, 7})
        s.speed_boost(True, "again")
        self.assertEqual(len(self.pinned), 1)
        s.load_boost(True, "teleport")
        self.assertEqual(self.pinned[-1], {3, 4, 5, 6, 7})
        s.load_boost(False, "moving")
        self.assertEqual(self.pinned[-1], {4, 5, 6, 7})      # back to the speed boost, not the server's own
        s.speed_boost(False, "slower")
        self.assertEqual(self.pinned[-1], {6, 7})

    def test_nothing_to_lend(self):
        self.s.speed_cpus = None
        self.s.speed_boost(True, "test")
        self.assertEqual(self.pinned, [])
        self.assertFalse(self.s.speed_on)


if __name__ == "__main__":
    unittest.main()
