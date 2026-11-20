"""cpupin: the core split and the loading boost's CPU sets."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpupin  # noqa: E402

CORES = [((0, c), [c, c + 8]) for c in range(8)]     # 8 cores, 16 CPUs, siblings c and c + 8


def fake_siblings(usable=None):
    return [(k, c) for k, c in CORES if usable is None or set(c) <= set(usable)]


class BoostTest(unittest.TestCase):
    def test_split_and_boost(self):
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings):
            server, game = cpupin.split()
            self.assertEqual(server, {6, 7, 14, 15})
            b = cpupin.boosted(server, game)
        self.assertTrue(server < b)
        self.assertEqual(b - server, {3, 4, 5, 11, 12, 13})     # the game's last three of six cores, both siblings
        self.assertLessEqual(len(b & game), len(game) // 2)

    def test_boost_from_a_pinned_process(self):
        # the server calls boosted() after pinning itself to its own CPUs: the game's are still found
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings), \
                mock.patch.object(cpupin.os, "sched_getaffinity", return_value={6, 7, 14, 15}, create=True):
            self.assertEqual(cpupin.boosted({6, 7, 14, 15}, {0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13}),
                             {3, 4, 5, 6, 7, 11, 12, 13, 14, 15})

    def test_nothing_to_lend(self):
        self.assertIsNone(cpupin.boosted(None, {1, 2}))
        self.assertIsNone(cpupin.boosted({1}, set()))

    @unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux")
    def test_pin_tree_keeps_this_process(self):
        cpus = os.sched_getaffinity(0)
        self.assertGreaterEqual(cpupin.pin_tree(cpus), 1)
        self.assertEqual(os.sched_getaffinity(0), cpus)


if __name__ == "__main__":
    unittest.main()
