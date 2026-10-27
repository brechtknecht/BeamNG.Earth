"""Driving fast (2026-10-25): a block's photo waits only as long as the drive to the block's edge allows
(Server._base_wait_s, Connection.edge_at), the server borrows some of the game's cores (Server.speed_boost), and the
photos' tiles of the blocks ahead are fetched early (Server.photo_ahead)."""
import sys
import time
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpupin  # noqa: E402
import earthd  # noqa: E402
import farfield  # noqa: E402
import quality  # noqa: E402


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


class PredictChainTest(unittest.TestCase):
    """The blocks after the next one (earthd.predict_chain: the prefetch when the follow blocks fall behind)."""
    rule = earthd.FOLLOW_RULES[512]

    def test_straight_drive_every_block_ahead(self):
        chain = earthd.predict_chain(0.0, 0.0, 55.0, 0.0, 0.0, 0.0, self.rule, 30.0)
        self.assertGreaterEqual(len(chain), 3)
        ts = [t for t, _ in chain]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(t <= 30.0 for t in ts))
        xs = [c[0] for _, c in chain]
        self.assertEqual(xs, sorted(set(xs)))                    # each further east, none twice
        self.assertTrue(all(c[1] == 0.0 for _, c in chain))
        # its first is the mod's next request (predict_request)
        self.assertEqual(chain[0][1], earthd.predict_request(0.0, 0.0, 55.0, 0.0, 0.0, 0.0, self.rule))

    def test_each_is_the_request_from_the_one_before(self):
        chain = earthd.predict_chain(0.0, 0.0, 40.0, 0.0, 0.0, 0.0, self.rule, 40.0)
        for (t0, a), (t1, b) in zip(chain, chain[1:]):
            px = 40.0 * t1 - 40.0 * earthd.FRAME_S          # the player just before it leaves a's prepare box
            self.assertLessEqual(abs(px - a[0]), self.rule.prepare + 1.0)
            self.assertEqual(b, earthd.predict_request(40.0 * t1, 0.0, 40.0, 0.0, *a, self.rule))

    def test_horizon_and_standing(self):
        self.assertEqual(earthd.predict_chain(0.0, 0.0, 55.0, 0.0, 0.0, 0.0, self.rule, 0.5), [])
        self.assertEqual(earthd.predict_chain(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, self.rule, 30.0), [])


class BaseWaitTest(unittest.TestCase):
    def wait(self, fast=False, edge_in=None):
        s = SimpleNamespace(fast=fast, edge_at=None if edge_in is None else time.monotonic() + edge_in,
                            quality=quality.Governor())
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

    def test_off_unless_asked(self):
        # (2026-10-27: the lent cores showed in the game's frames; BNGE_SPEED_BOOST=1 turns it back on)
        if "BNGE_SPEED_BOOST" not in os.environ:
            self.assertFalse(earthd.SPEED_BOOST)

    def test_nothing_to_lend(self):
        self.s.speed_cpus = None
        self.s.speed_boost(True, "test")
        self.assertEqual(self.pinned, [])
        self.assertFalse(self.s.speed_on)



class PhotoAheadTest(unittest.TestCase):
    def server(self):
        s = earthd.Server.__new__(earthd.Server)
        s.near_visual = "detail"
        s.layout = farfield.Layout(64, 512, True)
        s.photo_last, s.photo_asked, s.hm = None, {}, {}
        s.quality = quality.Governor()
        s.asked = []
        s.imagery = SimpleNamespace(prefetch=lambda frame, x0, y0, size, px: s.asked.append((x0 + size / 2, y0 + size / 2)))
        return s

    def test_blocks_ahead_nearest_first(self):
        s = self.server()
        anchor = SimpleNamespace(id=1, frame=None)
        s.photo_ahead(anchor, 0.0, 0.0, 50.0, 0.0)
        self.assertEqual(len(s.asked), earthd.PHOTO_AHEAD_BLOCKS)
        xs = [x for x, _ in s.asked]
        self.assertEqual(xs, sorted(xs))
        self.assertTrue(all(x > 0 and x % s.layout.grid == 0 and y == 0 for x, y in s.asked))
        s.photo_ahead(anchor, 50.0, 0.0, 50.0, 0.0)          # moved too little: no new round
        self.assertEqual(len(s.asked), earthd.PHOTO_AHEAD_BLOCKS)
        s.photo_ahead(anchor, 400.0, 0.0, 50.0, 0.0)         # on: only blocks not asked for yet
        self.assertEqual(len(set(s.asked)), len(s.asked))

    def test_standing_or_built_blocks(self):
        s = self.server()
        anchor = SimpleNamespace(id=1, frame=None)
        s.photo_ahead(anchor, 0.0, 0.0, 0.5, 0.0)
        self.assertEqual(s.asked, [])
        s.hm = {(1, float(c), 0.0): {} for c in range(0, 3000, 128)}
        s.photo_ahead(anchor, 0.0, 0.0, 50.0, 0.0)
        self.assertEqual(s.asked, [])


if __name__ == "__main__":
    unittest.main()
