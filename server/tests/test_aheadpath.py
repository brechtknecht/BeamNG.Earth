"""aheadpath: the way ahead of a driving player (the map data cells built ahead of it)."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aheadpath  # noqa: E402


class PointsTest(unittest.TestCase):
    def test_standing_has_no_path(self):
        self.assertEqual(aheadpath.points(5.0, 5.0, 0.5, 0.0), [])

    def test_length_follows_speed(self):
        town = aheadpath.points(0.0, 0.0, 10.0, 0.0)
        road = aheadpath.points(0.0, 0.0, 30.0, 0.0)
        motorway = aheadpath.points(0.0, 0.0, 70.0, 0.0)
        self.assertAlmostEqual(town[-1][1], aheadpath.MIN_M, delta=1)       # 900 m of 90 s: the minimum
        self.assertAlmostEqual(road[-1][1], 30.0 * aheadpath.LEAD_S, delta=1)
        self.assertAlmostEqual(motorway[-1][1], aheadpath.MAX_M, delta=1)
        self.assertAlmostEqual(road[-1][0], aheadpath.LEAD_S, delta=0.1)   # the time the player gets there

    def test_points_in_order_and_spaced(self):
        p = aheadpath.points(100.0, -50.0, 0.0, 25.0)
        self.assertEqual(p[0], (0.0, 100.0, -50.0, 0.0))
        ts = [q[0] for q in p]
        self.assertEqual(ts, sorted(ts))
        for a, b in zip(p, p[1:]):
            self.assertLessEqual(math.hypot(b[1] - a[1], b[2] - a[2]), aheadpath.STEP_M + 1e-6)
        self.assertTrue(all(abs(q[1] - 100.0) < 1e-6 for q in p))         # straight north
        self.assertLessEqual(p[-1][3], aheadpath.SPREAD_MAX_M)
        self.assertGreater(p[-1][3], p[1][3])

    def test_turn_bends_and_fades(self):
        left = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=0.1)
        right = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=-0.1)
        self.assertGreater(left[-1][2], 500.0)
        self.assertAlmostEqual(left[-1][2], -right[-1][2], delta=1e-6)
        # faded: the heading turns by at most turn * TURN_FADE_S (0.8 rad), never round a circle
        end_h = math.atan2(left[-1][2] - left[-2][2], left[-1][1] - left[-2][1])
        self.assertLess(end_h, 0.1 * aheadpath.TURN_FADE_S + 0.01)
        self.assertGreater(left[-1][1], 1000.0)

    def test_turn_capped(self):
        a = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=5.0)
        b = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=aheadpath.TURN_MAX)
        self.assertEqual(a, b)


class TrackTest(unittest.TestCase):
    def test_turn_rate(self):
        t = aheadpath.Track()
        for i in range(20):         # 0.2 rad/s to the left, a pose every 0.25 s
            h = 0.2 * i * 0.25
            t.add(i * 0.25, 7.5 * i, 0.0, 30 * math.cos(h), 30 * math.sin(h))
        self.assertAlmostEqual(t.turn(), 0.2, places=3)

    def test_heading_wraps(self):
        t = aheadpath.Track()
        for i, h in enumerate((math.pi - 0.1, math.pi - 0.05, -math.pi + 0.0, -math.pi + 0.05, -math.pi + 0.1)):
            t.add(i * 0.5, i * 15.0, 0.0, 30 * math.cos(h), 30 * math.sin(h))
        self.assertAlmostEqual(t.turn(), 0.1, places=3)

    def test_needs_a_second_and_restarts_on_a_jump_or_a_stop(self):
        t = aheadpath.Track()
        t.add(0.0, 0.0, 0.0, 30.0, 0.0)
        t.add(0.5, 15.0, 0.0, 0.0, 30.0)
        self.assertEqual(t.turn(), 0.0)                 # half a second: not yet
        t.add(1.0, 2000.0, 0.0, 30.0, 0.0)              # a teleport
        t.add(2.0, 2030.0, 0.0, 30.0, 0.0)
        self.assertEqual(t.turn(), 0.0)
        t.add(3.0, 2030.0, 0.0, 0.0, 0.0)               # stopped
        self.assertEqual(len(t.poses), 0)

    def test_steady_turn(self):
        """A bend going on: its turn; a corner taken (straight since): none."""
        tr = aheadpath.Track()
        for i in range(31):
            t = i * 0.1
            h = 0.05 * t
            tr.add(t, 0.0, 0.0, 50 * math.cos(h), 50 * math.sin(h))
        self.assertAlmostEqual(tr.steady_turn(), 0.05, places=2)
        tr = aheadpath.Track()
        for i in range(31):
            t = i * 0.1
            h = 0.5 * min(t, 1.0)
            tr.add(t, 0.0, 0.0, 50 * math.cos(h), 50 * math.sin(h))
        self.assertNotEqual(tr.turn(), 0.0)
        self.assertEqual(tr.steady_turn(), 0.0)
