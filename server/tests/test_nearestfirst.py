"""nearestfirst.NearestFirst: the job ranked lowest when a worker is free goes first; one not wanted never runs."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from nearestfirst import NearestFirst  # noqa: E402


def _blocked_executor(rank):
    """An executor whose one worker is held by a first job, so what is submitted next queues up."""
    ex = NearestFirst(rank)
    go, started = threading.Event(), threading.Event()

    def hold(_):
        started.set()
        go.wait(5)
    ex.submit(hold, "hold")
    assert started.wait(5)
    return ex, go


def test_lowest_rank_first_whatever_the_order():
    ranks = {"hold": 0, "far": 30, "near": 10, "mid": 20}
    ran = []
    ex, go = _blocked_executor(lambda k: ranks[k])
    futs = [ex.submit(ran.append, k) for k in ("far", "near", "mid")]
    go.set()
    for f in futs:
        f.result(5)
    ex.shutdown()
    assert ran == ["near", "mid", "far"]


def test_rank_is_asked_when_picking_not_when_submitted():
    ranks = {"hold": 0, "a": 1, "b": 2}
    ran = []
    ex, go = _blocked_executor(lambda k: ranks[k])
    futs = [ex.submit(ran.append, k) for k in ("a", "b")]
    ranks["a"], ranks["b"] = 2, 1       # the car moved on meanwhile
    go.set()
    for f in futs:
        f.result(5)
    ex.shutdown()
    assert ran == ["b", "a"]


def test_not_wanted_never_runs_and_its_future_gets_none():
    ranks = {"hold": 0, "stale": None, "next": 1}
    ran = []
    ex, go = _blocked_executor(lambda k: ranks[k])
    stale, nxt = ex.submit(ran.append, "stale"), ex.submit(ran.append, "next")
    go.set()
    assert stale.result(5) is None
    nxt.result(5)
    ex.shutdown()
    assert ran == ["next"] and ex.dropped == 1


def test_equal_ranks_in_arrival_order():
    ran = []
    ex, go = _blocked_executor(lambda k: 0)
    futs = [ex.submit(ran.append, k) for k in "abc"]
    go.set()
    for f in futs:
        f.result(5)
    ex.shutdown()
    assert ran == list("abc")


def test_a_rank_that_fails_keeps_the_job_and_an_error_reaches_the_future():
    def rank(k):
        if k == "odd":
            raise ValueError(k)
        return 1
    ex, go = _blocked_executor(rank)

    def boom(_):
        raise RuntimeError("boom")
    bad, ok = ex.submit(boom, "odd"), ex.submit(lambda k: k, "fine")
    go.set()
    try:
        bad.result(5)
        raise AssertionError("no error")
    except RuntimeError:
        pass
    assert ok.result(5) == "fine"
    ex.shutdown()


def test_shutdown_finishes_what_is_queued_and_refuses_more():
    ran = []
    ex, go = _blocked_executor(lambda k: 1)
    f = ex.submit(ran.append, "x")
    go.set()
    ex.shutdown()
    assert f.done() and ran == ["x"]
    assert ex.submit(ran.append, "late").cancelled()
