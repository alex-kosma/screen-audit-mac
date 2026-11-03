"""Opens the real app on a Mac for a few seconds and takes a picture of it:
the recorder with its menu bar icon, the main window, and a check-in popup.
GitHub's macOS runner does this on every push; the picture is uploaded with
the run. Also prints which windows each process put on screen, so the log
says whether the icon and windows really appeared.

    python3 tests/mac_show.py OUT_FOLDER
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def windows_of(pids):
    """Every on-screen window owned by these processes."""
    import Quartz
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") in pids:
            b = w.get("kCGWindowBounds") or {}
            out.append({"pid": w.get("kCGWindowOwnerPID"), "owner": str(w.get("kCGWindowOwnerName") or ""),
                        "name": str(w.get("kCGWindowName") or ""), "layer": w.get("kCGWindowLayer"),
                        "bounds": [round(b.get(k, 0)) for k in ("X", "Y", "Width", "Height")]})
    return out


def main(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="screen-audit-show-"))
    config = tmp / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp / "data"), "language": "en"}), encoding="utf-8")
    env = dict(os.environ, SCREEN_AUDIT_CONFIG=str(config))
    log = open(tmp / "processes.log", "w", encoding="utf-8")

    def start(*args):
        return subprocess.Popen([sys.executable, *args], cwd=str(HERE), env=env, stdout=log, stderr=subprocess.STDOUT)

    procs = {"recorder": start("screen_audit.py", "run")}
    time.sleep(3)
    procs["window"] = start("screen_audit_gui.py")
    time.sleep(3)
    procs["check-in"] = start("screen_audit.py", "checkin")
    time.sleep(8)

    subprocess.run(["screencapture", "-x", str(out_dir / "app-open.png")], check=False)
    status = {name: ("running" if p.poll() is None else f"exited {p.returncode}") for name, p in procs.items()}
    wins = windows_of({p.pid for p in procs.values()})
    names = {p.pid: name for name, p in procs.items()}

    # Stop it the way the window does, then make sure nothing is left.
    (tmp / "data" / "stop_request").touch()
    time.sleep(3)
    recorder_stopped = procs["recorder"].poll() is not None
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    log.close()

    print("Processes after 14 s:", json.dumps(status))
    print("Windows on screen:")
    for w in wins:
        print(f"  {names.get(w['pid'], w['pid']):9} layer={w['layer']:<3} {w['bounds']}  {w['owner']!r} {w['name']!r}")
    if not wins:
        print("  (none)")
    print("Recorder stopped on the stop request:", recorder_stopped)
    for f in (tmp / "data" / "errors.jsonl", tmp / "processes.log", tmp / "data" / "background.log"):
        if f.exists() and f.read_text(encoding="utf-8").strip():
            print(f"---- {f.name}")
            print(f.read_text(encoding="utf-8")[-4000:])

    # The run passes as long as nothing crashed; the picture is for people.
    crashed = [n for n, s in status.items() if s.startswith("exited") and n != "check-in"]
    if crashed:
        print("Crashed early:", crashed)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
