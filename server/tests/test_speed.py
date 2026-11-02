"""Driving fast (2026-10-25): Connection.edge_at, the time the game pauses at the committed block's edge (it feeds the
quality governor). The photo wait, speed boost and photo-ahead tests went with upstream's block pipeline (2026-11-02)."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import farfield  # noqa: E402


class EdgeAtTest(unittest.TestCase):
    def conn(self):
        return SimpleNamespace(block=farfield.Block(0.0, 0.0, 1024))    # collidable [-512, 511] (1 m samples)

    def test_time_to_the_pause(self):
        c = self.conn()
        at = earthd.Connection.edge_at(c, 0.0, 0.0, 50.0, 0.0)
        want = (511 - earthd.BASE_EDGE_STOP_M) / 50.0 - earthd.BASE_EDGE_LEAD_S
        self.assertAlmostEqual(at - time.monotonic(), want, delta=0.05)
        # diagonal: the nearer edge counts; west and south too
        at = earthd.Connection.edge_at(c, 300.0, 0.0, 30.0, -30.0)
        self.assertAlmostEqual(at - time.monotonic(), (511 - 15 - 300) / 30.0 - 0.5, delta=0.05)
        at = earthd.Connection.edge_at(c, 0.0, 0.0, 0.0, -40.0)
        self.assertAlmostEqual(at - time.monotonic(), (512 - 15) / 40.0 - 0.5, delta=0.05)

    def test_past_the_line_is_now(self):
        at = earthd.Connection.edge_at(self.conn(), 505.0, 0.0, 40.0, 0.0)
        self.assertAlmostEqual(at, time.monotonic(), delta=0.05)

    def test_slow_or_no_block(self):
        self.assertIsNone(earthd.Connection.edge_at(self.conn(), 0.0, 0.0, 10.0, 0.0))
        self.assertIsNone(earthd.Connection.edge_at(SimpleNamespace(block=None), 0.0, 0.0, 50.0, 0.0))
