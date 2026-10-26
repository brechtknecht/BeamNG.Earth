"""Detail and lookahead by the measured build budget (2026-10-26).

Microsoft Flight Simulator picks each tile's detail by the bandwidth it measures, so a slow line gets coarser
tiles in time instead of fine ones too late (Asobo, GDC 2022). Here the scarce thing is the server's build time:
the follow block the mod asks for must be ready before the moving player gets to the edge of the block in use
(edgeguard.lua pauses the game there, "Waiting for collision to load here"). The same work came for a Steam Deck
and a 16-thread PC, only later on the slow one, and at 200 km/h it came too late on an i7-9700K (2026-10-25).

Governor measures every follow block of a moving player: the time from the mod's request to terrain_ready (used)
against the time the player had left to its block's edge when it asked (budget, Server.edge_at). Their ratio is
the load (> 1: the game paused). A high load lowers the quality level one step at a time (at most every
UP_HOLD_S), a calm one (and a player standing) raises it again after CALM_S. Each level sheds work that is not on
the block's critical path and starts the critical work earlier:

    level 0   everything as built (the defaults)
    level 1   the speed boost (some of the game's cores for the server) at any speed; the prefetch builds the
              blocks after the next one too, as far ahead as one measured block build (block_ahead_s); far tiles
              one worker fewer
    level 2   two block builds ahead; the far field refines less (FAR_K); the block never waits for its photo
              (block_base brings it)
    level 3   three block builds ahead; the far field coarser still; far tiles one at a time

The cores come first: at 144 km/h in Munich the server's own 2 cores paused the game 9 times (98 s), with 4 cores
once (3 s) (2026-10-26). The road plans and photos are looked ahead longer (lead_scale) only while the blocks wait
on the road plans (their grading stage, plan_bound): more lookahead is more work at once, and a cloud drive at
200 km/h whose blocks were short of CPU (landcover, road paint) paused longer with it (2026-10-26). A block late on
its road plans (a fresh place's first cells: 55-80 s in Munich) lowers the level to 1 at most: less far field and
more blocks ahead do not bring its downloads sooner.

Pure apart from the clock; earthd asks it (Server.quality) at its decisions.
"""
import os
import threading
import time

ON = os.environ.get("BNGE_QUALITY", "1") != "0"     # 0: always level 0 (measured and logged, nothing shed)

LEVELS = 3
EMA = 0.4               # weight of the newest block in the load's moving average
UP_LOAD = 0.7           # load (the average and the newest block's) above this: one level lower in detail ...
UP_HOLD_S = 8.0         # ... at most every this many seconds (the next block shows the last step's effect)
DOWN_LOAD = 0.35        # load below this ...
CALM_S = 45.0           # ... for this long (and this long since the last step): one level back up
IDLE_S = 60.0           # no follow block measured this long (standing, slow): one level back up per CALM_S
MIN_BUDGET_S = 0.5      # a budget shorter than this counts as this (the player at the edge already)
BUILT_MIN_S = 0.5       # a block ready faster than this was prefetched: not a build time
BUILD_MAX_S = 15.0      # a build time counts as at most this for block_ahead_s
PLAN_SHARE = 0.4        # the blocks wait on their road plans (grading) at least this share of their budget ...
LEAD_STEP = 0.5         # ... : the lookahead's lead times (1 + LEAD_STEP * level)
FAR_K = (1.0, 1.0, 0.8, 0.65)        # the far field's refinement factors (farfield.K_TABLE) times this, by level
FAR_WORKERS_LESS = (0, 1, 1, None)   # far tile workers fewer by this, by level (None: one)


class Governor:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.level = 0
        self.load = None            # moving average of used / budget
        self.last_load = None
        self.blocks = self.late = 0     # follow blocks measured, of them later than their budget
        self.build_s = None         # moving average of a follow block's build time (not prefetched)
        self.plan = 0.0             # moving average of the blocks' road plan wait (grading s) / budget
        t = clock()
        self.step_t = t - max(UP_HOLD_S, CALM_S)    # the last level change
        self.calm_t = t             # since when the load is calm (below DOWN_LOAD)
        self.sample_t = t           # the last measured block

    def record(self, used_s, budget_s, plan_s=0.0):
        """A follow block was ready used_s after the mod asked, with budget_s left to the edge then (plan_s of its
        build in the grading stage: the road plans). Returns (old level, new level) when the level changed, else
        None."""
        with self.lock:
            now = self.clock()
            budget = max(MIN_BUDGET_S, budget_s)
            load = max(0.0, used_s) / budget
            if used_s >= BUILT_MIN_S:
                b = min(BUILD_MAX_S, used_s)
                self.build_s = b if self.build_s is None else EMA * b + (1 - EMA) * self.build_s
                self.plan = EMA * min(1.0, max(0.0, plan_s) / budget) + (1 - EMA) * self.plan
            self.last_load = load
            self.load = load if self.load is None else EMA * load + (1 - EMA) * self.load
            self.blocks += 1
            self.late += load > 1.0
            self.sample_t = now
            if self.load >= DOWN_LOAD:
                self.calm_t = now
            old = self.level
            # a block late on its road plans (a fresh place's downloads) is helped by the cores and the lookahead,
            # not by less far field: at most level 1 for it
            top = 1 if plan_s >= PLAN_SHARE * budget else LEVELS
            if not ON:
                top = 0
            if (min(self.load, load) > UP_LOAD or load > 1.0) and self.level < top and now - self.step_t >= UP_HOLD_S:
                self.level += 1
                self.step_t = now
            else:
                self._maybe_down(now)
            return (old, self.level) if self.level != old else None

    def tick(self):
        """Called with the player's poses: back up a level when calm or idle. (old, new) when it changed."""
        with self.lock:
            old = self.level
            self._maybe_down(self.clock())
            return (old, self.level) if self.level != old else None

    def _maybe_down(self, now):
        if self.level == 0 or now - self.step_t < CALM_S:
            return
        if now - self.calm_t >= CALM_S or now - self.sample_t >= IDLE_S:
            self.level -= 1
            self.step_t = now
            self.load = None if now - self.sample_t >= IDLE_S else self.load

    # --- what each level means -------------------------------------------------------------------------------------
    def plan_bound(self):
        """Do the follow blocks wait on their road plans (rather than on the CPU)?"""
        return self.plan >= PLAN_SHARE

    def lead_scale(self):
        """The road plans' and photos' lookahead (aheadpath.points lead_s, max_m) times this."""
        return 1.0 + LEAD_STEP * self.level if self.plan_bound() else 1.0

    def block_ahead_s(self):
        """How far ahead (s of driving) the prefetch builds blocks beyond the next one (earthd.predict_chain); 0:
        the next one only."""
        if self.level == 0 or self.build_s is None:
            return 0.0
        return self.level * self.build_s

    def far_k(self):
        """The far field's refinement factor K(L) times this."""
        return FAR_K[self.level]

    def far_workers(self, n):
        """Far tiles built at once out of n."""
        less = FAR_WORKERS_LESS[self.level]
        return 1 if less is None else max(1, n - less)

    def photo_waits(self):
        """Does a block wait for its base photo (until shortly before the edge, Server._base_wait_s)?"""
        return self.level < 2

    def boost(self):
        """The speed boost (more cores for the server) at any speed?"""
        return self.level >= 1

    def summary(self):
        load = "-" if self.load is None else f"{self.load:.2f}"
        build = "-" if self.build_s is None else f"{self.build_s:.1f} s"
        return (f"level {self.level}, load {load}, build {build}, plan wait {self.plan:.2f}, "
                f"{self.late}/{self.blocks} follow blocks late")
