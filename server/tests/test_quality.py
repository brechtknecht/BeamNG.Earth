"""Detail and lookahead by the measured build budget (quality.Governor)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.g = quality.Governor(self.clock)

    def test_in_time_keeps_full_detail(self):
        for _ in range(20):
            self.clock.t += 10
            self.assertIsNone(self.g.record(1.0, 8.0))
        self.assertEqual(self.g.level, 0)
        self.assertEqual(self.g.lead_scale(), 1.0)
        self.assertEqual(self.g.block_ahead_s(), 0.0)
        self.assertEqual(self.g.far_k(), 1.0)
        self.assertEqual(self.g.far_workers(3), 3)
        self.assertTrue(self.g.photo_waits())
        self.assertFalse(self.g.boost())

    def test_late_blocks_step_down_one_level_at_a_time(self):
        self.assertEqual(self.g.record(12.0, 6.0), (0, 1))     # the game paused: one level down at once
        self.assertTrue(self.g.boost())                         # (more cores first)
        self.assertIsNone(self.g.record(12.0, 6.0))            # ... not again before UP_HOLD_S
        self.clock.t += quality.UP_HOLD_S
        self.assertEqual(self.g.record(12.0, 6.0), (1, 2))
        self.assertFalse(self.g.photo_waits())
        self.assertTrue(self.g.boost())
        self.assertLess(self.g.far_k(), 1.0)
        for _ in range(5):
            self.clock.t += quality.UP_HOLD_S
            self.g.record(12.0, 6.0)
        self.assertEqual(self.g.level, quality.LEVELS)
        self.assertEqual(self.g.far_workers(3), 1)
        self.assertEqual(self.g.late, 8)
        self.assertAlmostEqual(self.g.block_ahead_s(), quality.LEVELS * 12.0)   # three block builds ahead
        self.assertFalse(self.g.plan_bound())                    # (no grading measured: the CPU is short)
        self.assertEqual(self.g.lead_scale(), 1.0)

    def test_waiting_on_road_plans_looks_further_ahead(self):
        for _ in range(3):
            self.clock.t += quality.UP_HOLD_S
            self.g.record(12.0, 6.0, plan_s=9.0)
        self.assertTrue(self.g.plan_bound())
        self.assertEqual(self.g.level, 1)                        # (no far field shed for the road plans)
        self.assertEqual(self.g.lead_scale(), 1.0 + quality.LEAD_STEP)
        self.clock.t += quality.UP_HOLD_S
        self.g.record(12.0, 6.0, plan_s=0.5)                     # late on the CPU: further down
        self.assertEqual(self.g.level, 2)

    def test_prefetched_blocks_are_no_build_time(self):
        self.g.record(12.0, 6.0)
        self.clock.t += quality.UP_HOLD_S
        self.g.record(0.0, 6.0)
        self.assertEqual(self.g.build_s, 12.0)

    def test_high_average_load_steps_down(self):
        for _ in range(6):
            self.clock.t += 10
            self.g.record(4.5, 6.0)                              # 0.75 of the budget, never late
        self.assertGreaterEqual(self.g.level, 1)
        self.assertEqual(self.g.late, 0)

    def test_calm_steps_back_up(self):
        self.g.record(12.0, 6.0)
        self.clock.t += quality.UP_HOLD_S
        self.g.record(12.0, 6.0)
        self.assertEqual(self.g.level, 2)
        for _ in range(8):                                       # in time again: the average falls ...
            self.clock.t += 5
            self.g.record(0.5, 6.0)
        self.assertEqual(self.g.level, 2)                        # ... and must stay calm CALM_S
        self.clock.t += quality.CALM_S
        self.assertEqual(self.g.record(0.5, 6.0), (2, 1))
        self.clock.t += 5
        self.assertIsNone(self.g.record(0.5, 6.0))              # CALM_S between the steps up
        self.clock.t += quality.CALM_S
        self.assertEqual(self.g.record(0.5, 6.0), (1, 0))

    def test_standing_steps_back_up(self):
        self.g.record(12.0, 6.0)
        self.assertIsNone(self.g.tick())
        self.clock.t += quality.IDLE_S
        self.assertEqual(self.g.tick(), (1, 0))

    def test_budget_at_the_edge(self):
        self.g.record(0.1, -4.0)                                 # the player waits at the edge already
        self.assertAlmostEqual(self.g.last_load, 0.1 / quality.MIN_BUDGET_S)


if __name__ == "__main__":
    unittest.main()
