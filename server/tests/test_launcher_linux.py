"""launcher.py on Linux: the user folder and start path with a Flatpak Steam, the game-closed grace period."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402
import launcher  # noqa: E402

FP = (".var", "app", "com.valvesoftware.Steam")


def touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@unittest.skipIf(os.name == "nt", "Linux: the launcher's Windows paths (LOCALAPPDATA, USERPROFILE) ignore HOME")
class FlatpakSteam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()
        # the player's report (2026-10-17): Debian, Steam as a Flatpak; an earlier launcher made ~/.local/share's
        # folder (the mod in it), the game itself writes inside the Flatpak's home
        self.host = os.path.join(self.home, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        os.makedirs(os.path.join(self.host, "mods"))
        self.flat = os.path.join(self.home, *FP, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        self.exe = os.path.join(self.home, *FP, ".local", "share", "Steam", "steamapps", "common", "BeamNG.drive",
                                "BinLinux", "BeamNG.drive.x64")
        touch(self.exe)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_folder_the_game_wrote_in(self):
        touch(os.path.join(self.flat, "beamng.log"))
        with mock.patch.object(launcher, "steam_compat_tool", return_value=""):
            self.assertEqual(launcher.linux_build(), ("native", self.flat))

    def test_steam_linux_runtime_is_the_native_build(self):
        # GitHub #56: Steam set to steamlinuxruntime_4 (a container for the native build), a stale Proton prefix
        # from 2024 with a log in it: the native folder, not the prefix
        pfx = os.path.join(self.home, *FP, ".local", "share", "Steam", "steamapps", "compatdata", str(launcher.APP_ID),
                           "pfx", "drive_c", "users", "steamuser", "AppData", "Local", "BeamNG.drive", "0.32")
        touch(os.path.join(pfx, "beamng.log"), time.time() - 3e7)
        touch(os.path.join(self.flat, "beamng.log"))
        for tool in ("steamlinuxruntime_4", "steamlinuxruntime_sniper"):
            with mock.patch.object(launcher, "steam_compat_tool", return_value=tool):
                self.assertEqual(launcher.linux_build(), ("native", self.flat), tool)
        self.assertTrue(launcher.is_proton("proton_9") and launcher.is_proton("GE-Proton9-20")
                        and launcher.is_proton("proton_experimental"))
        self.assertFalse(launcher.is_proton("steamlinuxruntime_4") or launcher.is_proton("") or launcher.is_proton(None))

    def test_game_mode_overlay(self):
        # a Steam Deck's Game Mode shows only windows Steam's overlay tags as the game: found in the Steam root
        lib = os.path.join(self.home, *FP, ".local", "share", "Steam", "ubuntu12_64", "gameoverlayrenderer.so")
        touch(lib)
        self.assertEqual(launcher.steam_overlay_lib(), lib)
        with mock.patch.dict(os.environ, {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}):
            self.assertTrue(launcher.under_gamescope())
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            os.environ.pop("GAMESCOPE_WAYLAND_DISPLAY", None)
            self.assertFalse(launcher.under_gamescope())

    def test_flatpak_folder_before_the_first_start(self):
        import shutil
        shutil.rmtree(os.path.join(self.home, ".local"))
        self.assertEqual(launcher.native_folder(), self.flat)

    def test_host_steam_keeps_local_share(self):
        os.makedirs(os.path.join(self.home, ".local", "share", "Steam"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_newest_log_wins(self):
        touch(os.path.join(self.flat, "beamng.log"), time.time() - 1000)
        touch(os.path.join(self.host, "beamng.log"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_flatpak_game_through_steam(self):
        self.assertEqual(install._steam_roots()[0], os.path.join(self.home, *FP, ".local", "share", "Steam"))
        self.assertEqual(launcher.linux_game_exe(), self.exe)
        self.assertTrue(launcher.sandboxed_game(self.exe))
        self.assertFalse(launcher.sandboxed_game("/usr/games/steam/BeamNG.drive.x64"))

    def test_flatpak_steam_pid(self):
        touch(os.path.join(self.home, *FP, ".steam", "steam.pid"))
        with open(os.path.join(self.home, *FP, ".steam", "steam.pid"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(launcher.steam_running())


class GameClosed(unittest.TestCase):
    def test_a_restart_during_the_start_is_not_a_close(self):
        seen = iter([[1], [], [], [2], [2], []] + [[]] * 100)
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(launcher, "game_pids", lambda: next(seen)), \
                mock.patch.object(launcher, "game_connected", lambda: False), \
                mock.patch.object(launcher.time, "sleep", sleep), \
                mock.patch.object(launcher.time, "time", lambda: clock[0]):
            launcher.wait_for_game(None)
        # [1] found; gone twice (6 s), back ([2] x2, 6 s), then gone for good: closed after GAME_GONE_S of absence
        self.assertGreaterEqual(clock[0], 12 + launcher.GAME_GONE_S)


@unittest.skipIf(os.name == "nt", "Linux: ensure_ca_certs leaves Windows alone")
class CaCerts(unittest.TestCase):
    """The packaged app's OpenSSL looks in /usr/lib/ssl (Ubuntu's): elsewhere SSL_CERT_FILE points at a bundle."""
    NONE = mock.Mock(cafile="/usr/lib/ssl/cert.pem", capath="/nonexistent/certs")

    def run_it(self, bundles, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch("ssl.get_default_verify_paths", return_value=self.NONE), \
                mock.patch.object(launcher, "CA_BUNDLES", bundles):
            if not env:
                os.environ.pop("SSL_CERT_FILE", None)
            launcher.ensure_ca_certs()
            return os.environ.get("SSL_CERT_FILE")

    def test_system_bundle(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(self.run_it(("/nonexistent/a.crt", f.name)), f.name)

    def test_certifi_fallback(self):
        import certifi
        self.assertEqual(self.run_it(("/nonexistent/a.crt",)), certifi.where())

    def test_kept_when_set(self):
        self.assertEqual(self.run_it((), {"SSL_CERT_FILE": "/mine.pem"}), "/mine.pem")


if __name__ == "__main__":
    unittest.main()


class WindowsTasklist(unittest.TestCase):
    """game_pids on Windows: tasklist's output in the console's code page (a German Windows, 2026-10-17: byte 0x81
    stopped the launcher with UnicodeDecodeError)."""

    def run_with(self, stdout):
        res = mock.Mock(stdout=stdout, returncode=0)
        with mock.patch.object(launcher, "IS_WIN", True), mock.patch.object(launcher.subprocess, "run", return_value=res):
            return launcher.game_pids()

    def test_localised_no_tasks_message(self):
        msg = "INFORMATION: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.\r\n".encode("cp850")
        self.assertIn(b"\x81", msg)
        self.assertEqual(self.run_with(msg), [])

    def test_game_found(self):
        exe = launcher.GAME_EXE_WIN
        out = f'"{exe}","4242","Console","1","1.234.567 K"\r\n'.encode("cp850")
        self.assertEqual(self.run_with(out), [4242])

    def test_no_output(self):
        self.assertEqual(self.run_with(None), [])


class FlatpakGameProcess(unittest.TestCase):
    """game_pids / game_alive on Linux when argv[0] does not name the game (a Flatpak Steam's container, a player's
    video 2026-10-17: the server stopped ~30 s after the game started)."""

    def fake_proc(self, procs):
        """procs: {pid: (argv list of bytes, exe path or None)}"""
        real_open, real_listdir, real_readlink = open, os.listdir, os.readlink

        def listdir(p):
            return [str(k) for k in procs] + ["self"] if p == "/proc" else real_listdir(p)

        def fopen(p, *a, **k):
            parts = str(p).split("/")
            if len(parts) == 4 and parts[1] == "proc" and parts[3] == "cmdline":
                import io
                return io.BytesIO(b"\0".join(procs[int(parts[2])][0]))
            return real_open(p, *a, **k)

        def readlink(p):
            parts = str(p).split("/")
            if len(parts) == 4 and parts[1] == "proc" and parts[3] == "exe":
                exe = procs[int(parts[2])][1]
                if exe is None:
                    raise PermissionError(p)
                return exe
            return real_readlink(p)
        return mock.patch.multiple(launcher.os, listdir=listdir, readlink=readlink), \
            mock.patch("builtins.open", fopen)

    def test_game_found_by_its_executable(self):
        game = "/home/u/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/BeamNG.drive/BinLinux/" + \
            os.path.basename(launcher.GAME_EXE_LINUX)
        procs = {
            10: ([b"/usr/lib/pressure-vessel/from-host/lib/ld-linux-x86-64.so.2", game.encode(), b"-forcecloud"], game),
            11: ([game.encode() + b" --type=renderer --enable-crash-reporter"], game),       # a CEF helper
            12: ([b"/usr/bin/bash", b"-c", b"x"], "/usr/bin/bash"),
            13: ([b"/usr/bin/other"], None),                                                     # not ours
        }
        p1, p2 = self.fake_proc(procs)
        with mock.patch.object(launcher, "IS_WIN", False), p1, p2:
            self.assertEqual(launcher.game_pids(), [10])

    def test_alive_while_the_mod_is_connected(self):
        port = format(launcher.SERVER_PORT, "04X")
        tcp = ("  sl  local_address rem_address   st\n"
               f"   0: 0100007F:{port} 0100007F:8A3C 01 00000000:00000000\n")
        with mock.patch.object(launcher, "IS_WIN", False), mock.patch.object(launcher, "game_pids", lambda: []), \
                mock.patch("builtins.open", mock.mock_open(read_data=tcp)):
            self.assertTrue(launcher.game_alive())
        listen = tcp.replace(" 01 ", " 0A ")
        with mock.patch.object(launcher, "IS_WIN", False), mock.patch.object(launcher, "game_pids", lambda: []), \
                mock.patch("builtins.open", mock.mock_open(read_data=listen)):
            self.assertFalse(launcher.game_alive())


class ConsoleEcho(unittest.TestCase):
    def test_only_warnings_errors_tracebacks_and_connected(self):
        log = ["2026-10-17 07:04:50,488 INFO earthd: session 1 kept\n",
               "earthd listening on 127.0.0.1:47613 session x\n",
               "2026-10-17 07:04:55,466 INFO earthd: client connected ('127.0.0.1', 34278)\n",
               "2026-10-17 07:05:01,000 WARNING earthd.net: net: host: circuit open for 60 s\n",
               "2026-10-17 07:05:02,000 ERROR earthd: tile failed\n",
               "Traceback (most recent call last):\n", '  File "dem.py", line 236\n', "dem.DemUnavailable: x\n",
               "2026-10-17 07:05:03,000 INFO earthd: anchor 1: land cover wait 3.11 s\n",
               "2026-10-17 07:05:04,000 INFO earthd: client connected ('127.0.0.1', 1)\n"]
        state = {}
        out = launcher.echo_lines(log[:4], state) + launcher.echo_lines(log[4:], state)
        self.assertEqual(out[0], "  Game          connected to the world server")
        self.assertTrue(out[1].startswith("  server: ") and "WARNING" in out[1])
        self.assertIn("ERROR", out[2])
        self.assertEqual([o.strip() for o in out[3:6]], ["server: Traceback (most recent call last):",
                                                         'server:   File "dem.py", line 236', "server: dem.DemUnavailable: x"])
        self.assertEqual(len(out), 6, "no INFO lines, 'connected' once")


class ModsOffAfterUpdateTest(unittest.TestCase):
    """A game update turns every mod off until the player turns them back on (BeamNG 0.40, 2026-10-19): the
    launcher reads the game's log and says what to do."""

    def test_the_line_is_reported(self):
        got = []
        old = launcher.problem
        launcher.problem = lambda msg, hint=None: got.append((msg, hint))
        self.addCleanup(setattr, launcher, "problem", old)
        with tempfile.TemporaryDirectory() as d:
            since = time.time() - 1
            launcher.watch_game_log(d, since)
            with open(os.path.join(d, "beamng.log"), "w") as f:
                f.write(" 1.2|I|getModsDisabledAfterUpdate| Mods will be disabled after updating from version: '0.39.4'\n")
            for _ in range(20):
                if got:
                    break
                time.sleep(0.25)
        self.assertEqual(len(got), 1)
        self.assertIn("turned all mods off", got[0][0])

    def test_last_runs_log_is_ignored(self):
        got = []
        old = launcher.problem
        launcher.problem = lambda msg, hint=None: got.append(msg)
        self.addCleanup(setattr, launcher, "problem", old)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "beamng.log")
            with open(p, "w") as f:
                f.write("Mods will be disabled after updating from version: '0.39.4'\n")
            os.utime(p, (time.time() - 3600, time.time() - 3600))      # an old run's log
            launcher.watch_game_log(d, time.time())
            time.sleep(3)
        self.assertEqual(got, [])
