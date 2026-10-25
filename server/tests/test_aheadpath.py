"""aheadpath: the way ahead of a driving player (the road plans' lookahead), and the block processes planning several
cells ahead at once."""
import math
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aheadpath  # noqa: E402
import blockworker  # noqa: E402


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


class FakeShared:
    def __init__(self, busy=()):
        self.busy_keys = set(busy)

    def busy(self, key):
        return key in self.busy_keys


class FakeGrader:
    def __init__(self, delay, shared=None):
        self.delay = delay
        self.shared = shared
        self.done = set()
        self.started = []
        self.lock = threading.Lock()

    def planned(self, key):
        with self.lock:
            return key in self.done

    def warm(self, key):
        with self.lock:
            self.started.append((key, time.monotonic()))
        time.sleep(self.delay)
        with self.lock:
            self.done.add(key)


class WarmLoopTest(unittest.TestCase):
    def setUp(self):
        self.old = blockworker._s

        class S:
            pass
        blockworker._s = S()
        blockworker._s.cv = threading.Condition()
        blockworker._s.want = []

    def tearDown(self):
        with blockworker._s.cv:
            blockworker._s.want = []
        blockworker._s = self.old

    def test_cells_planned_at_once_most_urgent_first(self):
        g = FakeGrader(0.4)
        keys = [(14, i, 0) for i in range(6)]
        with blockworker._s.cv:
            blockworker._s.want = [(g, k) for k in keys]
        threading.Thread(target=blockworker._warm_loop, daemon=True).start()
        end = time.monotonic() + 5
        while len(g.done) < len(keys) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(g.done, set(keys))
        order = [k for k, _ in g.started]
        self.assertEqual(sorted(order[:blockworker.WARM_THREADS]), keys[:blockworker.WARM_THREADS])
        t0 = g.started[0][1]
        self.assertLess(g.started[blockworker.WARM_THREADS - 1][1] - t0, 0.2)     # together, not one after another

    def test_cell_another_process_plans_is_left_to_it(self):
        g = FakeGrader(0.05, FakeShared(busy=[(14, 0, 0)]))
        with blockworker._s.cv:
            blockworker._s.want = [(g, (14, 0, 0)), (g, (14, 1, 0))]
        self.assertEqual(blockworker._next_warm(set()), (g, (14, 1, 0)))
        self.assertIsNone(blockworker._next_warm({(14, 1, 0)}))


if __name__ == "__main__":
    unittest.main()
