"""earthd.Server._quick_once: a block's stand-in photo is made once, whoever asks (2026-11-02: up to 5 of one block)."""
import os
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


class QuickOnceTest(unittest.TestCase):
    def server(self):
        d = tempfile.mkdtemp()
        return types.SimpleNamespace(out_dir=d, quick_lock=threading.Lock(), quick_made={}, quick_running={})

    def test_made_once(self):
        s, anchor, made = self.server(), types.SimpleNamespace(id=3), []

        def make():
            made.append(1)
            time.sleep(0.3)
            open(os.path.join(s.out_dir, "q.png"), "w").close()
            return "q.png"
        got = []
        ts = [threading.Thread(target=lambda: got.append(earthd.Server._quick_once(s, anchor, 1.0, 2.0, make)))
              for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(got, ["q.png"] * 3)
        self.assertEqual(earthd.Server._quick_once(s, anchor, 1.0, 2.0, make), "q.png")
        self.assertEqual(len(made), 1)
        os.remove(os.path.join(s.out_dir, "q.png"))     # (its file gone: made again)
        self.assertEqual(earthd.Server._quick_once(s, anchor, 1.0, 2.0, make), "q.png")
        self.assertEqual(len(made), 2)

    def test_none_and_errors_not_kept(self):
        s, anchor = self.server(), types.SimpleNamespace(id=3)
        self.assertIsNone(earthd.Server._quick_once(s, anchor, 0.0, 0.0, lambda: None))
        with self.assertRaises(ValueError):
            earthd.Server._quick_once(s, anchor, 0.0, 0.0, lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertEqual(s.quick_made, {})
        self.assertEqual(s.quick_running, {})


class PhotoSubmitTest(unittest.TestCase):
    def test_photos_after_the_blocks_in_a_block_process(self):
        """_photo_submit: a photo job in a block process waits for the blocks there (submit_low); elsewhere (the photo
        process) as any job (2026-11-08: blocks the car needed waited 8-13 s behind the photos)."""
        class Pool:
            def __init__(self):
                self.calls = []

            def submit(self, fn, *a):
                self.calls.append("submit")

            def submit_low(self, fn, *a):
                self.calls.append("low")
        req, pre, photo = Pool(), Pool(), Pool()
        s = types.SimpleNamespace(block_pools=[(req, None), (pre, None)])
        for pool in (req, pre, photo):
            earthd.Server._photo_submit(s, pool, abs, -1)
        self.assertEqual((req.calls, pre.calls, photo.calls), (["low"], ["low"], ["submit"]))


if __name__ == "__main__":
    unittest.main()
