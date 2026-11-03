"""Checks that run without a Mac: every module imports, and the logic that
doesn't touch macOS works. Run from the repo folder:

    python3 -m unittest discover tests
"""

import datetime as dt
import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="screen-audit-test-"))
os.environ["SCREEN_AUDIT_CONFIG"] = str(TMP / "config.json")
(TMP / "config.json").write_text(json.dumps({"data_dir": str(TMP / "data"), "language": "en"}), encoding="utf-8")

import macos  # noqa: E402
import screen_audit as sa  # noqa: E402

FIXED = dt.datetime(2026, 10, 28, 19, 0, 0)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TMP))
        self.cfg = sa.load_config()
        self.cfg["data_dir"] = self.root / "data"
        self.cfg["frames_dir"] = self.root / "data" / "frames"
        self.cfg["data_dir"].mkdir(parents=True)
        patches = [mock.patch.object(sa, "HERE", self.root), mock.patch.object(sa, "now", lambda: FIXED),
                   mock.patch.object(sa, "idle_seconds", lambda: 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Imports(unittest.TestCase):
    def test_modules_import(self):
        import make_icon  # noqa: F401
        try:
            import screen_audit_checkin  # noqa: F401
            import screen_audit_gui  # noqa: F401
        except ImportError as e:
            self.skipTest(f"pywebview not installed: {e}")

    def test_no_windows_leftovers(self):
        words = ["windll", "msvcrt", "winreg", "0x08000000", "CREATE_NO_WINDOW", "os.startfile", "pythonw",
                 "System.Windows.Forms", "powershell", ".lnk", "APPDATA", "taskkill",
                 "SetCurrentProcessExplicitAppUserModelID", "edgechromium"]
        for f in ["screen_audit.py", "screen_audit_gui.py", "screen_audit_checkin.py", "make_icon.py", "macos.py"]:
            text = (HERE / f).read_text(encoding="utf-8")
            for w in words:
                self.assertNotIn(w, text, f"{w} in {f}")


class Analyze(Base):
    def test_block_prompt(self):
        day = FIXED.strftime("%Y-%m-%d")
        start = FIXED.replace(hour=10, minute=0)
        rows = []
        for i in range(0, 15 * 60, 5):  # a full block, a frame every 30 s
            t = start + dt.timedelta(seconds=i)
            e = {"t": t.isoformat(), "state": "frame" if i % 30 == 0 else "same",
                 "proc": "Safari" if i < 600 else "Mail", "title": "Proposal - Google Docs" if i < 600 else "Inbox"}
            if e["state"] == "frame":
                e["frame"] = str(self.cfg["frames_dir"] / day / f"{t:%H%M%S}.jpg")
                e["side"] = [str(self.cfg["frames_dir"] / day / "100000-side2.jpg")]
            rows.append(e)
        write_jsonl(self.cfg["data_dir"] / "activity" / f"{day}.jsonl", rows)
        seen = {}

        def fake_claude(prompt, model, cwd, tools=False, **kw):
            seen.update(prompt=prompt, model=model, tools=tools, dirs=kw.get("extra_dirs"))
            return json.dumps({"summary": "Wrote a proposal, then email.", "activities": [], "question": "Why?"}), 0.03

        with mock.patch.object(sa, "claude", fake_claude):
            sa.cmd_analyze(self.cfg, None)
        p = seen["prompt"]
        self.assertIn("2026-10-28 10:00 to 10:15", p)
        self.assertIn("Safari: Proposal - Google Docs", p)
        self.assertIn("Mail: Inbox", p)
        frames = [line for line in p.splitlines() if line.startswith("- ") and line.endswith(".jpg") and "side" not in line]
        self.assertEqual(len(frames), self.cfg["frames_per_batch"])
        self.assertIn("100000-side2.jpg", p)
        self.assertTrue(seen["tools"])
        self.assertEqual(seen["model"], "haiku")
        out = sa.read_jsonl(self.cfg["data_dir"] / "analysis" / f"{day}.jsonl")
        self.assertEqual(out[0]["summary"], "Wrote a proposal, then email.")
        self.assertEqual(out[0]["cost_usd"], 0.03)

    def test_ukrainian_rule(self):
        self.cfg["language"] = "uk"
        self.assertIn("Ukrainian", sa.lang_rule(self.cfg, "analysis"))
        self.cfg["language"] = "en"
        self.assertEqual(sa.lang_rule(self.cfg, "analysis"), "")


class Daily(Base):
    def test_write_daily(self):
        day = "2026-10-28"
        write_jsonl(self.cfg["data_dir"] / "activity" / f"{day}.jsonl",
                    [{"t": f"{day}T10:00:{s:02d}", "state": "frame", "proc": "Safari"} for s in range(0, 60, 5)])
        write_jsonl(self.cfg["data_dir"] / "analysis" / f"{day}.jsonl", [{
            "start": f"{day}T10:00:00", "end": f"{day}T10:15:00", "summary": "Drafted a proposal.",
            "activities": [{"activity": "proposal", "minutes": 12, "category": "sales & applications",
                            "process": "client proposal"}]}])
        write_jsonl(self.cfg["data_dir"] / "checkins" / f"{day}.jsonl", [
            {"t": f"{day}T10:05:00", "question": "What for?", "window": "Docs", "answer": "For Acme", "blocker": "Waiting on prices"}])
        (self.cfg["data_dir"] / "wrapups").mkdir()
        (self.cfg["data_dir"] / "wrapups" / f"{day}.json").write_text(json.dumps(
            {"result": "answered", "done": "Call with Acme", "blockers": None, "tomorrow": "Send it"}), encoding="utf-8")
        (self.cfg["data_dir"] / "wrapups" / "2026-10-27.json").write_text(json.dumps(
            {"result": "answered", "tomorrow": "Start the proposal"}), encoding="utf-8")
        seen = {}

        def fake_claude(prompt, model, cwd, **kw):
            seen.update(prompt=prompt, model=model)
            return "## In short\nGood day.", 0.05

        with mock.patch.object(sa, "claude", fake_claude):
            out = sa.write_daily(self.cfg, day)
        self.assertEqual(out, self.root / "reports" / "daily" / f"{day}.md")
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Work log, Wednesday 2026-10-28"))
        self.assertIn("## In short", text)
        self.assertEqual(seen["model"], "sonnet")
        for bit in ("Drafted a proposal.", "blocker: Waiting on prices", "Finished today: Call with Acme",
                    'Yesterday they planned to start with: "Start the proposal"'):
            self.assertIn(bit, seen["prompt"])

    def test_nothing_to_write(self):
        with mock.patch.object(sa, "claude", side_effect=AssertionError("should not be called")):
            self.assertIsNone(sa.write_daily(self.cfg, "2026-10-01"))


class Questions(Base):
    def test_follow_up_from_recent_block(self):
        write_jsonl(self.cfg["data_dir"] / "analysis" / "2026-10-28.jsonl", [
            {"end": "2026-10-28T18:45:00", "question": "Why by hand?", "summary": "Copied invoices."}])
        self.assertEqual(sa.pick_question(self.cfg), ("Why by hand?", "Copied invoices."))

    def test_generic_when_follow_up_is_old_or_asked(self):
        write_jsonl(self.cfg["data_dir"] / "analysis" / "2026-10-28.jsonl", [
            {"end": "2026-10-28T16:00:00", "question": "Old?", "summary": ""}])
        q, ctx = sa.pick_question(self.cfg)
        self.assertIn(q, sa.GENERIC_QUESTIONS)
        self.assertEqual(ctx, "")
        self.cfg["language"] = "uk"
        self.assertIn(sa.pick_question(self.cfg)[0], sa.GENERIC_QUESTIONS_UK)

    def test_no_repeats_until_pool_used(self):
        asked = sa.GENERIC_QUESTIONS[:-1]
        write_jsonl(self.cfg["data_dir"] / "checkins" / "2026-10-28.jsonl", [{"question": q} for q in asked])
        self.assertEqual(sa.pick_question(self.cfg)[0], sa.GENERIC_QUESTIONS[-1])


class Wrapup(Base):
    def active_day(self, minutes):
        write_jsonl(self.cfg["data_dir"] / "activity" / "2026-10-28.jsonl",
                    [{"t": "x", "state": "frame"}] * int(minutes * 60 / self.cfg["interval_sec"]))

    def test_due_after_time_with_work(self):
        self.active_day(40)
        self.assertTrue(sa.wrapup_due(self.cfg))

    def test_not_before_time(self):
        self.active_day(40)
        self.cfg["wrapup_time"] = "19:30"
        self.assertFalse(sa.wrapup_due(self.cfg))

    def test_not_on_quiet_days_or_twice(self):
        self.active_day(10)
        self.assertFalse(sa.wrapup_due(self.cfg))
        self.active_day(40)
        sa.write_state(self.cfg, wrapup_offered="2026-10-28")
        self.assertFalse(sa.wrapup_due(self.cfg))

    def test_not_while_idle_paused_or_off(self):
        self.active_day(40)
        with mock.patch.object(sa, "idle_seconds", lambda: 300):
            self.assertFalse(sa.wrapup_due(self.cfg))
        (self.cfg["data_dir"] / "paused_until").write_text("forever")
        self.assertFalse(sa.wrapup_due(self.cfg))
        (self.cfg["data_dir"] / "paused_until").unlink()
        self.cfg["wrapup_enabled"] = False
        self.assertFalse(sa.wrapup_due(self.cfg))


class Mac(Base):
    def test_single_instance(self):
        a = sa.single_instance(self.cfg, "x")
        self.assertIsNotNone(a)
        self.assertIsNone(sa.single_instance(self.cfg, "x"))
        a.close()
        b = sa.single_instance(self.cfg, "x")
        self.assertIsNotNone(b)
        b.close()

    def test_language(self):
        with mock.patch.object(macos, "preferred_language", lambda: "uk-UA"):
            self.assertEqual(sa.detect_language(), "uk")
        with mock.patch.object(macos, "preferred_language", lambda: "en-GB"):
            self.assertEqual(sa.detect_language(), "en")

    def test_preferred_language_parsing(self):
        out = '(\n    "uk-UA",\n    "en-US"\n)\n'
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=out)):
            self.assertEqual(macos.preferred_language(), "uk-UA")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="(\n    en,\n    uk\n)\n")):
            self.assertEqual(macos.preferred_language(), "en")

    def test_display_for(self):
        rects = [(0, 0, 1512, 982), (1512, -200, 4072, 1240)]
        self.assertEqual(macos.display_for((100, 100, 800, 600), rects), rects[0])
        self.assertEqual(macos.display_for((2000, 0, 800, 600), rects), rects[1])
        self.assertEqual(macos.display_for((1400, 100, 400, 300), rects), rects[1])  # centre on the right one
        self.assertEqual(macos.display_for(None, rects), rects[0])
        self.assertEqual(macos.display_for((9000, 9000, 10, 10), rects), rects[0])

    def test_popup_position(self):
        try:
            import screen_audit_checkin as c
        except ImportError as e:
            self.skipTest(f"pywebview not installed: {e}")
        frame = (0, 0, 1512, 982)
        visible = (0, 69, 1512, 888)  # Dock 69 at the bottom, menu bar 25 at the top
        self.assertEqual(c.popup_position(frame, visible, (440, 470)), (1512 - 440 - 24, 25 + 888 - 470 - 24))
        self.assertEqual(c.popup_position(frame, visible, (460, 600), centre=True), (526, 25 + 144))

    def test_launch_agent(self):
        agent = self.root / "LaunchAgents" / "x.plist"
        with mock.patch.object(macos, "LAUNCH_AGENT", agent), \
                mock.patch.object(macos, "APP_BUNDLE", self.root / "missing.app"):
            macos.set_start_at_login(True, log=self.root / "bg.log")
            self.assertTrue(macos.start_at_login_enabled())
            plist = plistlib.loads(agent.read_bytes())
            self.assertTrue(plist["RunAtLoad"])
            self.assertEqual(plist["ProgramArguments"][1:], [str(HERE / "screen_audit.py"), "run"])
            macos.set_start_at_login(False)
            self.assertFalse(agent.exists())

    def test_icns(self):
        import make_icon
        out = self.root / "x.icns"
        make_icon.draw().save(out, format="ICNS")
        self.assertEqual(out.read_bytes()[:4], b"icns")

    def test_default_privacy(self):
        names = {p.lower() for p in sa.DEFAULTS["exclude_processes"]}
        for app in ("keychain access", "1password", "bitwarden", "passwords"):
            self.assertIn(app, names)
        self.assertTrue(sa.is_excluded(self.cfg, "", "Keychain Access"))
        self.assertTrue(sa.is_excluded(self.cfg, "Chase - Log in", "Safari"))
        self.assertFalse(sa.is_excluded(self.cfg, "Proposal", "Pages"))

    def test_translated_app_names_excluded_by_bundle_id(self):
        # Passwords and Keychain Access as a Ukrainian Mac names them
        self.assertTrue(sa.is_excluded(self.cfg, "", "Паролі", "com.apple.Passwords"))
        self.assertTrue(sa.is_excluded(self.cfg, "", "Зв'язка ключів", "com.apple.keychainaccess"))
        self.assertFalse(sa.is_excluded(self.cfg, "", "Нотатки", "com.apple.Notes"))

    def test_next_run_clears_what_a_killed_run_left(self):
        (self.cfg["data_dir"] / "paused_until").write_text("forever")
        sa.stop_request_file(self.cfg).touch()
        sa.end_of_run(self.cfg)
        self.assertIsNone(sa.paused_until(self.cfg))
        self.assertFalse(sa.stop_request_file(self.cfg).exists())

    def test_claude_runs_with_its_own_folder_on_path(self):
        env = macos.env_for("/Users/x/.nvm/versions/node/v20.11.0/bin/claude")
        self.assertTrue(env["PATH"].startswith("/Users/x/.nvm/versions/node/v20.11.0/bin" + os.pathsep))
        custom = self.root / "bin" / "claude"
        custom.parent.mkdir()
        custom.touch()
        custom.chmod(0o755)
        self.assertEqual(sa.find_claude(str(custom)), str(custom))

    def test_newest_nvm_node_first(self):
        home = self.root / "home"
        for v in ("v9.11.2", "v20.11.0", "v18.19.1"):
            (home / ".nvm" / "versions" / "node" / v / "bin").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual([Path(b).parent.name for b in macos._nvm_bins()], ["v20.11.0", "v18.19.1", "v9.11.2"])

    def test_screen_recording_error_is_translated(self):
        self.cfg["language"] = "uk"
        cap = sa.Capturer(self.cfg)
        cap.report_permission()
        cap.report_permission()  # once an hour, not every tick
        rows = sa.read_jsonl(self.cfg["data_dir"] / "errors.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], sa.TEXT["uk"]["err_screen_recording"])



class WindowConfig(unittest.TestCase):
    def test_window_saves_to_the_file_the_engine_reads(self):
        try:
            import screen_audit_gui as gui
        except ImportError:
            self.skipTest("needs pywebview")
        self.assertEqual(str(gui.CONFIG), os.environ["SCREEN_AUDIT_CONFIG"])


class ClaudeCheck(Base):
    def run_with(self, returncode=0, stdout="", stderr=""):
        fake = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(sa, "find_claude", lambda path="": "/usr/local/bin/claude"), \
                mock.patch("subprocess.run", return_value=fake) as run:
            return sa.check_claude(self.cfg), run

    def test_missing(self):
        with mock.patch.object(sa, "find_claude", lambda path="": None):
            self.assertEqual(sa.check_claude(self.cfg)["kind"], "missing")

    def test_ok(self):
        r, run = self.run_with(stdout=json.dumps({"is_error": False, "result": "OK"}))
        self.assertEqual((r["ok"], r["kind"]), (True, "ok"))
        self.assertIn("haiku", run.call_args.args[0])

    def test_not_logged_in(self):
        r, _ = self.run_with(returncode=1, stdout=json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"}))
        self.assertEqual((r["ok"], r["kind"]), (False, "login"))
        r, _ = self.run_with(returncode=1, stderr="Credit balance is too low")
        self.assertEqual(r["kind"], "login")

    def test_other_error(self):
        r, _ = self.run_with(returncode=1, stderr="Connection refused")
        self.assertEqual((r["kind"], r["message"]), ("error", "Connection refused"))

    def test_timeout(self):
        with mock.patch.object(sa, "find_claude", lambda path="": "/x/claude"), \
                mock.patch("subprocess.run", side_effect=sa.subprocess.TimeoutExpired("claude", 90)):
            self.assertEqual(sa.check_claude(self.cfg)["kind"], "error")


class Storage(Base):
    def test_sizes_and_waiting_days(self):
        try:
            import screen_audit_gui as gui
        except ImportError as e:
            self.skipTest(f"pywebview not installed: {e}")
        today = dt.date.today()
        for i, size in ((0, 1000), (5, 3000), (6, 2000)):  # today, and two days past the 3-day keep
            d = self.cfg["frames_dir"] / f"{today - dt.timedelta(days=i)}"
            d.mkdir(parents=True)
            (d / "120000.jpg").write_bytes(b"x" * size)
        (self.cfg["frames_dir"] / "not-a-day").mkdir()
        (self.cfg["data_dir"] / "activity").mkdir()
        (self.cfg["data_dir"] / "activity" / "a.jsonl").write_bytes(b"y" * 500)
        (self.root / "reports").mkdir()
        (self.root / "reports" / "r.md").write_bytes(b"z" * 70)
        sa.write_state(self.cfg, analyzed_until=f"{today - dt.timedelta(days=6)}T10:00:00")
        api = gui.Api.__new__(gui.Api)
        api._cfg = self.cfg
        with mock.patch.object(gui, "HERE", self.root):
            s = api.storage()
        self.assertEqual(s["frames"], 6000)
        self.assertEqual(s["days"], 3)
        self.assertEqual(s["first"], f"{today - dt.timedelta(days=6)}")
        self.assertEqual(s["reports"], 70)
        self.assertGreaterEqual(s["data"], 500)
        self.assertLess(s["data"], 6000)  # screenshots inside data/ aren't counted twice
        self.assertEqual(s["waiting_days"], 2)
        self.assertIsNotNone(s["free"])



class Codex(Base):
    def fake_run(self, answer="OK", returncode=0, stderr=""):
        seen = {}

        def run(cmd, **kw):
            seen.update(cmd=cmd, input=kw.get("input"))
            # what Codex would see: the files behind the -i options, while they exist
            seen["attached"] = [Path(cmd[i + 1]).resolve() for i, a in enumerate(cmd) if a == "-i"]
            seen["instructions"] = next((Path(json.loads(a.split("=", 1)[1])).read_text() for a in cmd
                                         if a.startswith("model_instructions_file=")), None)
            if "-o" in cmd and answer is not None:
                Path(cmd[cmd.index("-o") + 1]).write_text(answer, encoding="utf-8")
            return mock.Mock(returncode=returncode, stdout="", stderr=stderr)
        return run, seen

    def call(self, **kw):
        run, seen = self.fake_run(kw.pop("answer", "the answer"))
        with mock.patch.object(sa, "find_codex", lambda path="": "/opt/homebrew/bin/codex"), \
                mock.patch("subprocess.run", side_effect=run):
            out = sa.codex("Summarise.", self.root, **kw)
        return out, seen

    def test_codex_call_is_locked_down(self):
        (text, cost), seen = self.call(model="gpt-x", effort="low")
        self.assertEqual((text, cost), ("the answer", 0))
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["/opt/homebrew/bin/codex", "exec"])
        for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules", "read-only",
                     'web_search="disabled"', "project_doc_max_bytes=0"):
            self.assertIn(flag, cmd)
        disabled = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--disable"}
        for feature in ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "hooks", "browser_use", "computer_use"):
            self.assertIn(feature, disabled)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertEqual(seen["input"], "Summarise.")
        self.assertEqual(seen["instructions"], sa.CODEX_SYSTEM)  # replaces Codex's own coding-agent instructions

    def test_images_with_commas_and_missing_files(self):
        folder = self.root / "Work, Archive"
        folder.mkdir()
        a, b = folder / "a.jpg", folder / "b.jpg"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        _, seen = self.call(images=[a, folder / "gone.jpg", b])
        cmd = seen["cmd"]
        attached = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
        self.assertEqual(len(attached), 2)  # the missing one is left out instead of failing the call
        self.assertTrue(all("," not in x for x in attached))
        self.assertEqual(seen["attached"], [a.resolve(), b.resolve()])  # same order, same files
        self.assertEqual(cmd[-4:-2][0], "-i")  # images last, prompt on stdin

    def test_codex_failure_and_empty_answer(self):
        with mock.patch.object(sa, "find_codex", lambda path="": "/x/codex"):
            run, _ = self.fake_run(None, returncode=1, stderr="Not logged in")
            with mock.patch("subprocess.run", side_effect=run), self.assertRaisesRegex(RuntimeError, "Not logged in"):
                sa.codex("x", self.root)
            run, _ = self.fake_run("  ")
            with mock.patch("subprocess.run", side_effect=run), self.assertRaisesRegex(RuntimeError, "no answer"):
                sa.codex("x", self.root)
        with mock.patch.object(sa, "find_codex", lambda path="": None), self.assertRaisesRegex(RuntimeError, "not found"):
            sa.codex("x", self.root)

    def test_analysis_goes_to_codex_with_images_attached(self):
        self.cfg["ai"] = "codex"
        day = FIXED.strftime("%Y-%m-%d")
        start = FIXED.replace(hour=10, minute=0)
        rows = [{"t": (start + dt.timedelta(seconds=i)).isoformat(), "state": "frame", "proc": "Pages", "title": "Plan",
                 "frame": str(self.cfg["frames_dir"] / day / f"{i:06d}.jpg")} for i in range(0, 900, 30)]
        write_jsonl(self.cfg["data_dir"] / "activity" / f"{day}.jsonl", rows)
        seen = {}

        def fake_codex(prompt, cwd, model="", effort=None, images=(), **kw):
            seen.update(prompt=prompt, effort=effort, model=model, images=list(images))
            return json.dumps({"summary": "Planned.", "activities": [], "question": None}), 0

        with mock.patch.object(sa, "codex", fake_codex), \
                mock.patch.object(sa, "claude", side_effect=AssertionError("Claude should not be called")):
            sa.cmd_analyze(self.cfg, None)
        self.assertNotIn("Read tool", seen["prompt"])
        self.assertIn("attached as images", seen["prompt"])
        self.assertEqual(len(seen["images"]), self.cfg["frames_per_batch"])
        self.assertEqual((seen["effort"], seen["model"]), ("low", "gpt-6-luna"))  # the small model for summaries
        self.assertEqual(sa.read_jsonl(self.cfg["data_dir"] / "analysis" / f"{day}.jsonl")[0]["summary"], "Planned.")

    def test_claude_prompt_unchanged(self):
        claude_intro, claude_side = sa.FRAMES_INTRO["claude"]
        self.assertEqual(claude_intro, "Read each one with the Read tool:")
        self.assertTrue(claude_side.startswith("Read these too"))

    def test_models_and_effort_per_job(self):
        self.cfg["ai"] = "codex"
        calls = []
        with mock.patch.object(sa, "codex", lambda prompt, cwd, model="", effort=None, **kw:
                               (calls.append((model, effort)), ("x", 0))[1]):
            sa.ask(self.cfg, "analysis", "p")
            sa.ask(self.cfg, "daily", "p")
            sa.ask(self.cfg, "report", "p")
            sa.ask(self.cfg, "report", "p", model="gpt-big")  # report --model
        self.assertEqual(calls, [("gpt-6-luna", "low"), ("", "medium"), ("", "high"), ("gpt-big", "high")])
        self.assertEqual(sa.model_label(self.cfg, "daily"), "Codex")
        self.assertEqual(sa.model_label(self.cfg, "report", "gpt-big"), "Codex gpt-big")
        self.cfg["ai"] = "claude"
        self.assertEqual(sa.model_label(self.cfg, "report"), "opus")

    def test_check_codex(self):
        def status(rc, out):
            return mock.Mock(returncode=rc, stdout=out, stderr="")

        def check(login_rc, login_out, request=None):
            with mock.patch.object(sa, "find_codex", lambda path="": "/x/codex"), \
                    mock.patch("subprocess.run", return_value=status(login_rc, login_out)), \
                    mock.patch.object(sa, "codex", request or (lambda *a, **k: ("OK", 0))):
                return sa.check_codex(self.cfg)

        self.assertEqual(check(0, "Logged in using ChatGPT\n")["kind"], "ok")
        self.assertEqual(check(1, "Not logged in\n")["kind"], "login")
        # a broken config also exits 1: say what's wrong instead of "log in"
        r = check(1, "Error loading config.toml: invalid key")
        self.assertEqual(r["kind"], "error")
        self.assertIn("config.toml", r["message"])

        # a saved login that no longer works shows up on the real request
        def refused(*a, **k):
            raise RuntimeError("codex exited 1: 401 Unauthorized: token expired")
        self.assertEqual(check(0, "Logged in using ChatGPT", refused)["kind"], "login")

        def broken(*a, **k):
            raise RuntimeError("codex exited 1: stream disconnected")
        self.assertEqual(check(0, "Logged in using an API key", broken)["kind"], "error")
        with mock.patch.object(sa, "find_codex", lambda path="": None):
            self.assertEqual(sa.check_codex(self.cfg)["kind"], "missing")

    def test_custom_cli_path_must_be_a_program(self):
        folder = self.root / "bin"
        folder.mkdir()
        exe = folder / "codex"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        self.assertEqual(sa.find_cli("codex", str(exe)), str(exe))
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(sa.find_cli("codex", str(folder)))  # a folder isn't a program


class Config(unittest.TestCase):
    def load(self, user):
        path = Path(tempfile.mkdtemp(dir=TMP)) / "config.json"
        path.write_text(json.dumps({"language": "en", "data_dir": str(TMP / "d"), **user}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SCREEN_AUDIT_CONFIG": str(path)}):
            return sa.load_config()

    def test_partial_per_job_override_keeps_the_rest(self):
        cfg = self.load({"codex_effort": {"report": "xhigh"}, "codex_models": {"report": "gpt-6-astra"}})
        self.assertEqual(cfg["codex_effort"], {"analysis": "low", "daily": "medium", "report": "xhigh"})
        self.assertEqual(cfg["codex_models"]["analysis"], "gpt-6-luna")

    def test_wrong_type_is_ignored_and_defaults_stay_untouched(self):
        cfg = self.load({"codex_effort": "high"})
        self.assertEqual(cfg["codex_effort"]["analysis"], "low")
        cfg["codex_effort"]["analysis"] = "max"
        self.assertEqual(sa.DEFAULTS["codex_effort"]["analysis"], "low")


class AiCheckRace(unittest.TestCase):
    def test_switching_ai_mid_check_checks_the_new_one(self):
        try:
            import screen_audit_gui as gui
        except ImportError as e:
            self.skipTest(f"pywebview not installed: {e}")
        import threading
        api = gui.Api.__new__(gui.Api)
        api._cfg = {"ai": "claude", "data_dir": TMP / "race"}
        api._claude, api._claude_busy, api._claude_again = {}, False, False
        release = threading.Event()
        checked = []

        def check_ai(cfg):
            checked.append(cfg["ai"])
            if len(checked) == 1:
                release.wait(5)  # the first (Claude) check is slow
            return {"ok": True, "kind": "ok", "ai": cfg["ai"]}

        with mock.patch.object(gui.sa, "check_ai", check_ai), mock.patch.object(gui.sa, "write_state"):
            api.check_claude()
            api._cfg = {"ai": "codex", "data_dir": TMP / "race"}  # Settings saved with Codex
            api.check_claude()
            self.assertTrue(api._claude_status()["checking"])  # no Claude result passed off as Codex's
            release.set()
            for _ in range(100):
                if not api._claude_busy:
                    break
                threading.Event().wait(0.05)
        self.assertEqual(checked, ["claude", "codex"])
        self.assertEqual(api._claude_status()["ai"], "codex")
        self.assertFalse(api._claude_status()["checking"])


class Tray(Base):
    def test_cues_are_wav(self):
        import io
        import wave
        for freqs in sa.CUES.values():
            with wave.open(io.BytesIO(sa.cue_wav(freqs))) as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertGreater(w.getnframes(), 0)

    def errors(self, *rows):
        write_jsonl(self.cfg["data_dir"] / "errors.jsonl", list(rows))

    def test_problem_alert(self):
        since = (FIXED - dt.timedelta(minutes=5)).isoformat()
        alerted = {"since": since, "last": None}
        self.errors({"t": (FIXED - dt.timedelta(hours=1)).isoformat(), "where": "analyze", "error": "old"},
                    {"t": FIXED.isoformat(), "where": "capture", "error": "not a summary"})
        self.assertIsNone(sa.problem_alert(self.cfg, alerted))  # from before this run, or not about summaries
        self.errors({"t": FIXED.isoformat(), "where": "analyze",
                     "error": "RuntimeError('claude exited 1: Not logged in. Please run /login')"})
        title, text = sa.problem_alert(self.cfg, alerted)
        self.assertIn("summaries are failing", title)
        self.assertIn("claude /login", text)
        self.assertIsNone(sa.problem_alert(self.cfg, alerted))  # not again within ALERT_EVERY_H

    def test_problem_alert_names_codex(self):
        self.cfg["ai"] = "codex"
        self.errors({"t": FIXED.isoformat(), "where": "daily", "error": "RuntimeError('codex exited 1: boom')"})
        _, text = sa.problem_alert(self.cfg, {"since": "", "last": None})
        self.assertTrue(text.startswith("Codex failed:"), text)

    def test_claude_failure_reason_from_json(self):
        r = mock.Mock(returncode=1, stdout='{"result": "Not logged in", "total_cost_usd": 0}', stderr="")
        with mock.patch.object(sa, "find_claude", return_value="/bin/claude"), \
                mock.patch.object(sa.subprocess, "run", return_value=r):
            with self.assertRaisesRegex(RuntimeError, "claude exited 1: Not logged in"):
                sa.claude("hi", "m", self.root)



class Doctor(Base):
    def run_doctor(self, state=None, running=False, activity=None, ai=None):
        if state:
            sa.write_state(self.cfg, **state)
        if activity:
            write_jsonl(self.cfg["data_dir"] / "activity" / f"{FIXED:%Y-%m-%d}.jsonl", activity)
        with mock.patch.object(sa, "is_running", return_value=running), \
                mock.patch.object(sa, "check_ai", return_value=ai or {"ok": True, "kind": "ok"}), \
                mock.patch.object(macos, "start_at_login_enabled", return_value=False):
            return {c["name"]: c for c in sa.doctor_checks(self.cfg)}

    def test_not_running(self):
        checks = self.run_doctor()
        self.assertEqual(checks["Recorder"]["status"], "fail")
        self.assertIn("Screen Audit.app", checks["Recorder"]["fix"])
        self.assertEqual(checks["Screen Recording"]["status"], "info")  # a terminal's answer doesn't count

    def test_permissions_come_from_the_recorder(self):
        tick = {"t": FIXED.isoformat(), "state": "no_permission"}
        checks = self.run_doctor(state={"run_pid": 42, "permissions": {"screen": False, "accessibility": True, "pid": 42}},
                                 running=True, activity=[tick])
        self.assertEqual(checks["Screen Recording"]["status"], "fail")
        self.assertIn("restart", checks["Screen Recording"]["fix"])
        self.assertEqual(checks["Accessibility"]["status"], "ok")
        self.assertEqual(checks["Capturing"]["status"], "fail")

    def test_ai_login_fix(self):
        checks = self.run_doctor(ai={"ok": False, "kind": "login", "message": "Not logged in"})
        c = checks["Claude (writes the summaries)"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("claude", c["fix"])

    def test_failed_summaries_warn(self):
        write_jsonl(self.cfg["data_dir"] / "errors.jsonl",
                    [{"t": FIXED.isoformat(), "where": "analyze", "error": "RuntimeError('claude exited 1: x')"}])
        self.assertEqual(self.run_doctor()["Summaries"]["status"], "warn")


class Requests(Base):
    def test_restart_request_is_cleared_by_the_next_run(self):
        sa.restart_request_file(self.cfg).parent.mkdir(parents=True, exist_ok=True)
        sa.restart_request_file(self.cfg).touch()
        sa.end_of_run(self.cfg)
        self.assertFalse(sa.restart_request_file(self.cfg).exists())

    def test_context_starts_from_the_example_and_stays_put(self):
        (self.root / "context.example.md").write_text("- (example)\n", encoding="utf-8")
        p = sa.context_file()
        self.assertEqual(p.read_text(encoding="utf-8"), "- (example)\n")
        p.write_text("- my own list\n", encoding="utf-8")
        self.assertEqual(sa.context_file().read_text(encoding="utf-8"), "- my own list\n")

    def test_summaries_load_no_claude_md_and_keep_no_transcript(self):
        r = mock.Mock(returncode=0, stdout='{"result": "ok", "total_cost_usd": 0}', stderr="")
        with mock.patch.object(sa, "find_claude", return_value="/bin/claude"), \
                mock.patch.object(sa.subprocess, "run", return_value=r) as run:
            sa.claude("hi", "m", self.root)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertIn("--no-session-persistence", cmd)


if __name__ == "__main__":
    unittest.main()
