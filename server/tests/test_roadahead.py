import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import roadahead  # noqa: E402


def make(lines):
    ra = roadahead.RoadAhead(None, lambda key: None)
    ra.lines = [np.array(L, np.float64) for L in lines]
    ra._index()
    ra.load = lambda *a: 0          # no frame / cells here
    return ra


class RoadAheadTest(unittest.TestCase):
    def test_follows_the_bend_and_the_straightest_continuation(self):
        # east along y = 0 to x = 100, then a junction: a line north (the bend) and a line south-east (sharper)
        ra = make([[(0, 0), (100, 0)], [(100, 0), (100, 200)], [(100, 0), (0, -100)]])
        pts = ra.path(10.0, 2.0, 1.0, 0.0, 200.0)
        self.assertAlmostEqual(pts[0][2], 0.0)
        far = pts[-1]
        self.assertAlmostEqual(far[0], 100.0, delta=2.0)    # turned north at the junction, not straight on
        self.assertGreater(far[1], 80.0)

    def test_drives_a_line_backwards_against_its_drawing_order(self):
        ra = make([[(200, 0), (0, 0)]])
        pts = ra.path(150.0, 0.0, -1.0, 0.0, 100.0)
        self.assertLess(pts[-1][0], 60.0)

    def test_no_road_goes_straight_on(self):
        ra = make([])
        pts = ra.path(0.0, 0.0, 0.0, 1.0, 64.0)
        self.assertEqual(len(pts), 5)
        self.assertAlmostEqual(pts[-1][1], 64.0)
        self.assertTrue(all(abs(p[0]) < 1e-9 for p in pts))

    def test_a_road_across_the_heading_is_not_the_cars(self):
        ra = make([[(0, -50), (0, 50)]])        # north-south, the car heads east
        pts = ra.path(0.0, 0.0, 1.0, 0.0, 48.0)
        self.assertAlmostEqual(pts[-1][0], 48.0)
        self.assertAlmostEqual(pts[-1][1], 0.0)

    def test_spacing(self):
        ra = make([[(0, 0), (1000, 0)]])
        pts = ra.path(0.0, 0.0, 1.0, 0.0, 160.0)
        s = [p[2] for p in pts]
        self.assertEqual(s, [i * roadahead.STEP_M for i in range(len(s))])
        for (ax, ay, _), (bx, by, _) in zip(pts, pts[1:]):
            self.assertAlmostEqual(math.hypot(bx - ax, by - ay), roadahead.STEP_M, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
