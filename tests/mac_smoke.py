"""Calls the real macOS pieces once and reports what came back. Run on a Mac
(GitHub's macOS runner does it on every push):

    python3 tests/mac_smoke.py

A required check that raises or returns the wrong shape fails the run.
Optional checks (screenshots, which need Screen Recording) only report.
With GITHUB_STEP_SUMMARY set, the results also go there as a table.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SCREEN_AUDIT_CONFIG", str(Path(tempfile.mkdtemp()) / "config.json"))

results = []  # (name, required, ok, detail)


def check(name, required=True):
    def run(fn):
        try:
            detail = fn()
            results.append((name, required, True, detail))
        except Exception as e:
            results.append((name, required, False, f"{type(e).__name__}: {e}"))
            traceback.print_exc()
        return fn
    return run


@check("pyobjc frameworks import")
def _():
    import AppKit, ApplicationServices, Foundation, Quartz  # noqa: E401,F401
    return "Quartz, AppKit, Foundation, ApplicationServices"


import macos  # noqa: E402


@check("idle_seconds")
def _():
    v = macos.idle_seconds()
    assert isinstance(v, float) and v >= 0, v
    return f"{v:.1f} s"


@check("screen_locked")
def _():
    v = macos.screen_locked()
    assert isinstance(v, bool), v
    return v


@check("display_rects")
def _():
    rects = macos.display_rects()
    assert rects and all(len(r) == 4 and r[2] > r[0] and r[3] > r[1] for r in rects), rects
    return rects


@check("display_for")
def _():
    rects = macos.display_rects()
    r = macos.display_for((rects[0][0] + 10, rects[0][1] + 10, 100, 100), rects)
    assert r == rects[0], r
    return r


@check("foreground")
def _():
    bounds, title, proc, bundle = macos.foreground()
    assert bounds is None or len(bounds) == 4, bounds
    assert isinstance(title, str) and isinstance(proc, str) and isinstance(bundle, str)
    return f"app={proc!r} bundle={bundle!r} title={title!r} bounds={bounds}"


@check("frontmost_app and give_focus_back")
def _():
    app = macos.frontmost_app()
    macos.give_focus_back(app)
    return str(app.bundleIdentifier()) if app is not None else None


@check("main run loop turns without the menu bar icon")
def _():
    n = [0]

    def alive():
        n[0] += 1
        return n[0] <= 2
    macos.run_main_loop_while(alive)
    return f"{n[0] - 1} turns"


deadline = [time.monotonic() + 5]


@check("signal handler under the run loop")
def _():
    import signal
    got = []
    macos.on_signal(signal.SIGUSR1, lambda *a: got.append(1))
    os.kill(os.getpid(), signal.SIGUSR1)
    macos.run_main_loop_while(lambda: not got and time.monotonic() < deadline[0])
    assert got, "handler never ran"
    return "delivered"


@check("permissions")
def _():
    s, a = macos.screen_recording_granted(), macos.accessibility_granted()
    assert s in (True, False, None) and a in (True, False, None), (s, a)
    return f"screen recording={s}, accessibility={a}"


@check("system_dark")
def _():
    v = macos.system_dark()
    assert isinstance(v, bool), v
    return v


@check("preferred_language")
def _():
    v = macos.preferred_language()
    assert isinstance(v, str), v
    return repr(v)


@check("screen_audit language and config")
def _():
    import screen_audit as sa
    cfg = sa.load_config()
    assert cfg["language"] in sa.LANGUAGES
    return cfg["language"]


@check("screenshots of every display at once", required=False)
def _():
    rects = macos.display_rects()
    shots = macos.grab_all(rects)
    assert set(shots) == set(rects)
    return "; ".join(f"{img.size[0]}x{img.size[1]} pixels for a {r[2] - r[0]}x{r[3] - r[1]} point display "
                     f"({img.size[0] / (r[2] - r[0]):.1f}x)" for r, img in shots.items())


@check("capture tick")
def _():
    import screen_audit as sa
    cfg = sa.load_config()
    cfg["data_dir"] = Path(tempfile.mkdtemp())
    cfg["frames_dir"] = cfg["data_dir"] / "frames"
    cfg["idle_skip_sec"] = 10 ** 9  # nobody types on a runner; take the screenshot anyway
    sa.Capturer(cfg).tick()
    rows = [r for p in (cfg["data_dir"] / "activity").glob("*.jsonl") for r in sa.read_jsonl(p)]
    assert rows, "nothing logged"
    keep = os.environ.get("SMOKE_SCREENSHOTS")  # the workflow uploads this folder
    if keep:
        import shutil
        Path(keep).mkdir(parents=True, exist_ok=True)
        for r in rows:
            for f in [r.get("frame")] + (r.get("side") or []):
                if f:
                    shutil.copy2(f, Path(keep) / Path(f).name)
    return f"state={rows[-1]['state']}"


@check("copy to clipboard")
def _():
    macos.copy_text("screen audit clipboard test")
    got = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    assert got == "screen audit clipboard test", got
    return "ok"


@check("Claude check answers")
def _():
    import screen_audit as sa
    cfg = sa.load_config()
    cfg["data_dir"] = Path(tempfile.mkdtemp())
    r = sa.check_claude(cfg, timeout=60)
    assert r["kind"] in ("ok", "missing", "login", "error"), r
    return f"kind={r['kind']} (a runner has no Claude login, so 'missing' or 'login' is expected)"


@check("Codex options and lockdown")
def _():
    """Fails when Codex is installed but no longer takes an option the app
    passes, or when the lockdown leaves a tool that runs commands or reads
    files. Passes with a note when Codex isn't installed."""
    import json
    import re
    import screen_audit as sa
    exe = sa.find_codex()
    if not exe:
        return "codex not installed, nothing to check"
    h = subprocess.run([exe, "exec", "--help"], capture_output=True, text=True, timeout=60)
    assert h.returncode == 0 and h.stdout, f"codex exec --help failed: {(h.stderr or h.stdout)[:300]}"
    unknown = [f for f in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--sandbox",
                           "--color", "--image", "--model", "--output-last-message", "--cd", "--config", "--disable")
               if f not in h.stdout]
    assert not unknown, f"codex exec no longer knows {unknown}"
    with tempfile.TemporaryDirectory() as t:
        ins = Path(t) / "instructions.md"
        ins.write_text(sa.CODEX_SYSTEM)
        r = subprocess.run([exe, "debug", "prompt-input", *sa.codex_lockdown(ins), "hi"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"codex rejected the lockdown options: {(r.stderr or r.stdout)[:300]}"
        items = json.loads(r.stdout)
    names = []
    for it in items:
        for ns in it.get("tools") or []:
            for tool in ns.get("tools", []):
                names.append(tool["name"])
                names += re.findall(r"### `([^`]+)`", tool.get("description", ""))
    left = [n for n in sa.CODEX_TOOLS_OFF if n in names]
    assert not left, f"the lockdown leaves {left}"
    text = json.dumps(items)
    assert "careful analyst" in text and "You are Codex" not in text, "Codex's own instructions are still in use"
    status = sa.check_codex(sa.load_config(), timeout=30)["kind"]
    return f"options known, no command or file tools left ({len(names)} tools), login check: {status}"


@check("popup placement")
def _():
    import screen_audit_checkin as c
    x, y = c.place((440, 470))
    assert isinstance(x, int) and isinstance(y, int), (x, y)
    return f"check-in at ({x}, {y}), wrap-up at {c.place((460, 600), centre=True)}"


@check("pywebview cocoa backend imports")
def _():
    import webview.platforms.cocoa  # noqa: F401
    import screen_audit_gui  # noqa: F401
    return "ok"


@check("menu bar icon builds")
def _():
    import threading
    import screen_audit as sa
    icon, setup = sa.make_tray(sa.load_config(), threading.Event(), threading.Event())
    return type(icon).__module__


@check("app bundle")
def _():
    import make_icon
    dest = Path(tempfile.mkdtemp()) / "Screen Audit.app"
    make_icon.build_app(dest, HERE / "screen_audit.icns")
    subprocess.run(["plutil", "-lint", str(dest / "Contents" / "Info.plist")], check=True, capture_output=True)
    subprocess.run(["bash", "-n", str(dest / "Contents" / "MacOS" / "Screen Audit")], check=True)
    return "Info.plist valid, launcher script parses"


@check("login item plist")
def _():
    agent = Path(tempfile.mkdtemp()) / "agent.plist"
    old = macos.LAUNCH_AGENT
    macos.LAUNCH_AGENT = agent
    try:
        macos.set_start_at_login(True)
        subprocess.run(["plutil", "-lint", str(agent)], check=True, capture_output=True)
        return plistlib.loads(agent.read_bytes())["ProgramArguments"]
    finally:
        macos.LAUNCH_AGENT = old


@check("pause and resume sounds play", required=False)
def _():
    import screen_audit as sa
    for name in ("pause", "resume"):
        assert macos.play_wav(sa.cue_wav(sa.CUES[name])), f"afplay failed on the {name} tone"
    return "afplay played both (a runner has no speakers, so 'not available' is fine there)"


@check("notification accepted", required=False)
def _():
    assert macos.notify("Screen Audit: smoke test", "Notification check from mac_smoke.py"), "osascript refused"
    return "osascript took it (whether a banner shows is up to Notification settings)"


def recorder(*args, env, timeout=30):
    return subprocess.run([sys.executable, str(HERE / "screen_audit.py"), *args], env=env, capture_output=True,
                          text=True, timeout=timeout)


@check("stop and restart through the command line")
def _():
    import json
    import screen_audit as sa
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.json").write_text(json.dumps({"data_dir": str(tmp / "data"), "language": "en"}), encoding="utf-8")
    env = {**os.environ, "SCREEN_AUDIT_CONFIG": str(tmp / "config.json")}
    cfg = {"data_dir": tmp / "data"}

    def wait(cond, secs=20):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.25)
        return False

    first = subprocess.Popen([sys.executable, str(HERE / "screen_audit.py"), "run", "--no-tray"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait(lambda: sa.is_running(cfg) and sa.read_state(cfg).get("run_pid") == first.pid), "never started"
        perms = sa.read_state(cfg).get("permissions")
        assert perms and perms["pid"] == first.pid, perms
        r = recorder("restart", env=env)
        assert "Restarted" in r.stdout, r.stdout + r.stderr
        assert first.wait(timeout=10) == 0, "first recorder didn't exit cleanly"
        second = sa.read_state(cfg)["run_pid"]
        assert second != first.pid and sa.is_running(cfg), second
        r = recorder("stop", env=env)
        assert "Stopped" in r.stdout and wait(lambda: not sa.is_running(cfg)), r.stdout + r.stderr
        return f"pid {first.pid} restarted as {second}, then stopped"
    finally:
        if first.poll() is None:
            first.kill()
        pid = sa.read_state(cfg).get("run_pid")
        if pid and sa.is_running(cfg):
            macos.kill(pid)


@check("doctor reports every check")
def _():
    import json
    r = recorder("doctor", "--json", "--no-ai", env=os.environ, timeout=60)
    checks = json.loads(r.stdout)
    names = {c["name"] for c in checks}
    for need in ("Python", "Packages", "App", "Recorder", "Screen Recording", "Accessibility", "Summaries"):
        assert need in names, (need, names)
    assert all(c["status"] in ("ok", "warn", "fail", "info") for c in checks), checks
    assert r.returncode == (1 if any(c["status"] == "fail" for c in checks) else 0), r.returncode
    return ", ".join(f"{c['name']}={c['status']}" for c in checks)


def main():
    lines = ["| Check | Needed | Result | Detail |", "|---|---|---|---|"]
    failed = False
    for name, required, ok, detail in results:
        mark = "pass" if ok else ("FAIL" if required else "not available")
        failed |= required and not ok
        print(f"{mark:14} {name}: {detail}")
        lines.append(f"| {name} | {'yes' if required else 'no'} | {mark} | {str(detail).replace('|', '/')[:300]} |")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## macOS smoke test\n\n" + "\n".join(lines) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
