"""The macOS pieces Screen Audit needs: idle time, the app and window in front,
the displays, screenshots, permissions, appearance, language, opening files,
background processes and start at login.

The pyobjc imports sit inside the functions, so the rest of the app (and its
tests) still imports on a machine without them.
"""

import glob
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "Screen Audit"
APP_BUNDLE = Path.home() / "Applications" / f"{APP_NAME}.app"
LAUNCH_AGENT_LABEL = "com.screenaudit.recorder"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

# Started from the Dock or at login, a process gets launchd's short PATH
# (/usr/bin:/bin:/usr/sbin:/sbin), which has neither `claude` nor the `node`
# an npm install of it needs. These are where the installers put them.
EXTRA_PATH = ["~/.local/bin", "~/.claude/local", "~/.codex/bin", "/opt/homebrew/bin", "/usr/local/bin",
              "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin",
              "~/.asdf/shims", "~/.local/share/mise/shims", "~/.local/share/fnm/aliases/default/bin"]


def _nvm_bins():
    """nvm keeps each Node version in its own folder; newest first."""
    def version(d):
        return tuple(int(x) for x in re.findall(r"\d+", Path(d).parent.name)[:3])
    return sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), key=version, reverse=True)


def extend_path():
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in [os.path.expanduser(x) for x in EXTRA_PATH] + _nvm_bins():
        if p not in parts and os.path.isdir(p):
            parts.append(p)
    os.environ["PATH"] = os.pathsep.join(x for x in parts if x)


def env_for(exe):
    """The environment to run a CLI in: its own folder first on PATH, so an
    npm-installed `claude` finds the `node` installed beside it."""
    env = dict(os.environ)
    folder = os.path.dirname(exe)
    if folder:
        env["PATH"] = folder + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- input and windows

def idle_seconds():
    import Quartz
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)


def screen_locked():
    """True on the lock screen, or while another user has the console."""
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary()
    if not d:
        return True  # no window-server session at all (logged out, SSH)
    return bool(d.get("CGSSessionScreenIsLocked")) or d.get("kCGSSessionOnConsoleKey") is False


def pump_run_loop():
    """NSWorkspace only learns which app is in front from notifications
    delivered on the main run loop. With the tray icon that loop is running;
    without it (run --no-tray) the capture loop is on the main thread, so it
    turns the loop briefly itself."""
    if threading.current_thread() is not threading.main_thread():
        return
    from Foundation import NSDate, NSRunLoop
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))


def _ax_title(pid):
    """The focused window's title through Accessibility, or "" without it."""
    try:
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            return ""
        app = AS.AXUIElementCreateApplication(pid)
        err, win = AS.AXUIElementCopyAttributeValue(app, AS.kAXFocusedWindowAttribute, None)
        if err or win is None:
            return ""
        err, title = AS.AXUIElementCopyAttributeValue(win, AS.kAXTitleAttribute, None)
        return str(title) if not err and title else ""
    except Exception:
        return ""


def foreground():
    """(bounds, title, app name, bundle id) of the window in front. bounds is
    (x, y, w, h) in global display points, or None when the app has no window
    on screen. The app name is in the Mac's language; the bundle id
    (com.apple.Passwords) is the same everywhere.
    Titles from the window list need Screen Recording; Accessibility gives
    them for the focused window even where the list leaves them out."""
    import Quartz
    from AppKit import NSWorkspace
    pump_run_loop()
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None, "", "", ""
    pid, name = app.processIdentifier(), str(app.localizedName() or "")
    bundle = str(app.bundleIdentifier() or "")
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    front = None
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
            front = w  # the list runs front to back
            break
    bounds = None
    if front is not None:
        b = front.get("kCGWindowBounds") or {}
        bounds = (b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0))
    title = _ax_title(pid) or str((front or {}).get("kCGWindowName") or "")
    return bounds, title, name, bundle


def frontmost_app():
    """The app in front right now, to hand focus back to later."""
    try:
        from AppKit import NSWorkspace
        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None


def give_focus_back(app):
    """A popup should sit on top without taking the keyboard from whatever the
    person is typing in. pywebview activates its own app when a window first
    shows, so this reactivates the app that was in front before."""
    if app is None:
        return
    try:
        from AppKit import NSRunningApplication
        if app.processIdentifier() == NSRunningApplication.currentApplication().processIdentifier():
            return
        on_main_thread(lambda: app.activateWithOptions_(0))
    except Exception:
        pass


def run_main_loop_while(alive):
    """Keep the main run loop turning while alive() is true. With no menu bar
    icon, this is what lets NSWorkspace notice which app is in front."""
    from Foundation import NSDate, NSRunLoop
    while alive():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))


def on_signal(signum, handler):
    """A signal handler that still runs while AppKit's run loop holds the main
    thread (Python's own handlers wait for Python code to run there)."""
    try:
        from PyObjCTools import MachSignals
        MachSignals.signal(signum, handler)
    except ImportError:
        import signal
        signal.signal(signum, handler)


# ---------------------------------------------------------------- displays

def display_rects():
    """(left, top, right, bottom) of every active display in global points,
    the main display first. Mirrored copies are left out."""
    import Quartz
    main = Quartz.CGMainDisplayID()
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    ids = list(ids[:count]) if not err and ids else [main]
    rects = []
    for d in sorted(ids, key=lambda d: d != main):
        if Quartz.CGDisplayMirrorsDisplay(d) != Quartz.kCGNullDirectDisplay:
            continue
        r = Quartz.CGDisplayBounds(d)
        x, y = int(r.origin.x), int(r.origin.y)
        rects.append((x, y, x + int(r.size.width), y + int(r.size.height)))
    return rects


def display_for(bounds, rects=None):
    """The display a window is on: the one holding its centre, else the one it
    overlaps most, else the main display."""
    rects = rects or display_rects()
    if not bounds:
        return rects[0]
    x, y, w, h = bounds
    cx, cy = x + w / 2, y + h / 2
    for r in rects:
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return r

    def overlap(r):
        return max(0, min(r[2], x + w) - max(r[0], x)) * max(0, min(r[3], y + h) - max(r[1], y))
    best = max(rects, key=overlap)
    return best if overlap(best) else rects[0]


def grab(rect):
    """Screenshot of one display rectangle (global points); see grab_all."""
    return grab_all([rect])[rect]


def grab_all(rects):
    """Screenshots of several display rectangles (global points) at each
    display's own pixel resolution, so a Retina screen comes back at 2x.
    One `screencapture` per display, all started at once, so the displays
    are captured at the same moment. Each display gets its own -R rectangle
    rather than one multi-file call, whose file order isn't tied to these
    rectangles. (PIL.ImageGrab runs the same tool, but versions before 11.2
    shrink a bbox grab down to points.)"""
    from PIL import Image
    jobs = {}
    try:
        for rect in rects:
            left, top, right, bottom = rect
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            jobs[rect] = (path, subprocess.Popen(
                ["screencapture", "-x", "-t", "png", "-R", f"{left},{top},{right - left},{bottom - top}", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        images = {}
        for rect, (path, proc) in jobs.items():
            try:
                _, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise OSError("screencapture took longer than 20 s")
            if proc.returncode != 0 or os.path.getsize(path) == 0:
                raise OSError(f"screencapture failed: {(err or b'').decode(errors='replace')[:200]}")
            with Image.open(path) as im:
                im.load()
                images[rect] = im.convert("RGB")
        return images
    finally:
        for path, proc in jobs.values():
            if proc.poll() is None:
                proc.kill()
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------- permissions

SETTINGS_PANES = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}


def screen_recording_granted():
    """True or False; None when macOS can't say (before 11). macOS decides
    this once per process, so a change shows after a restart."""
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except (ImportError, AttributeError):
        return None


def accessibility_granted():
    try:
        import ApplicationServices as AS
        return bool(AS.AXIsProcessTrusted())
    except (ImportError, AttributeError):
        return None


def request_permission(kind):
    """Put this app in the System Settings list (macOS asks once), then open
    the right pane so the person can switch it on."""
    try:
        if kind == "screen":
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()
        elif kind == "accessibility":
            import ApplicationServices as AS
            AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    subprocess.Popen(["open", SETTINGS_PANES[kind]])


# ---------------------------------------------------------------- look and language

def system_dark():
    r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip().lower() == "dark"


def preferred_language():
    """The first language in System Settings > General > Language & Region,
    like "uk-UA" or "en-GB", or "" if it can't be read."""
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r'"?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)"?', r.stdout.replace("(", " ")) if r.returncode == 0 else None
    return m.group(1) if m else ""


def app_icon(png=HERE / "screen_audit.png"):
    """The Dock icon for a pywebview window (otherwise Python's rocket)."""
    try:
        from AppKit import NSApplication, NSImage
        img = NSImage.alloc().initWithContentsOfFile_(str(png))
        if img:
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        pass


def menu_bar_only():
    """No Dock icon for the recorder; it lives in the menu bar."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def on_main_thread(fn, *args):
    """Run fn on the main (AppKit) thread; AppKit isn't safe from others."""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn, *args)
    except ImportError:
        fn(*args)


def bring_to_front():
    try:
        from AppKit import NSApplication
        on_main_thread(lambda: NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
    except Exception:
        pass


# ---------------------------------------------------------------- files and processes

def open_path(path):
    """Open a file or folder the way Finder would. A Markdown file with no app
    set for it opens in TextEdit instead of failing."""
    r = subprocess.run(["open", str(path)], capture_output=True)
    if r.returncode != 0 and Path(path).is_file():
        subprocess.run(["open", "-t", str(path)], capture_output=True)


def spawn(script, *args, log=None):
    """Run one of the scripts in the background, in its own session so it
    outlives the window or terminal that started it."""
    out = open(log, "a", encoding="utf-8") if log else subprocess.DEVNULL
    try:
        subprocess.Popen([sys.executable, str(HERE / script), *args], cwd=str(HERE), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    finally:
        if log:
            out.close()


def play_wav(data):
    """Play WAV bytes and return whether it played, once done. afplay needs a file."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(data)
    try:
        return subprocess.run(["/usr/bin/afplay", f.name], capture_output=True, timeout=10).returncode == 0
    finally:
        os.unlink(f.name)


def notify(title, text):
    """A Notification Center banner; pystray can't post one on macOS. The text goes in as
    arguments, so quotes in it need no escaping. True if macOS took it, which doesn't
    mean it was shown: banners can be switched off (under Script Editor)."""
    return subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                           "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                           title, text], capture_output=True, timeout=10).returncode == 0


def copy_text(text):
    subprocess.run(["pbcopy"], input=text, text=True, check=False)


def dir_size(path, skip=()):
    """Bytes in a folder and everything under it, leaving out the folders in
    skip. Missing or unreadable parts count as nothing."""
    total, skip = 0, {str(s) for s in skip}
    stack = [str(path)]
    while stack:
        d = stack.pop()
        if d in skip:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def kill(pid):
    import signal
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def volume_name(path):
    """The volume a path is on, or None for the startup disk."""
    p = Path(path).resolve()
    if len(p.parts) > 2 and p.parts[1] == "Volumes":
        return p.parts[2]
    return None


# ---------------------------------------------------------------- start at login

def launch_agent_args():
    """Through the app bundle when there is one, so the recorder runs as
    Screen Audit and uses the permissions given to it; otherwise Python
    directly."""
    if APP_BUNDLE.exists():
        return ["/usr/bin/open", "-g", "-n", "-a", str(APP_BUNDLE), "--args", "record"]
    return [sys.executable, str(HERE / "screen_audit.py"), "run"]


def start_at_login_enabled():
    return LAUNCH_AGENT.exists()


def set_start_at_login(on, log=None):
    """A LaunchAgent that starts the recorder (with its menu bar icon, not the
    window) when this user logs in. Takes effect from the next login."""
    if not on:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    plist = {"Label": LAUNCH_AGENT_LABEL, "ProgramArguments": launch_agent_args(),
             "RunAtLoad": True, "WorkingDirectory": str(HERE), "ProcessType": "Interactive",
             "LimitLoadToSessionType": "Aqua"}
    if log:
        plist["StandardOutPath"] = plist["StandardErrorPath"] = str(log)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)
