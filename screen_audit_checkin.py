"""Screen Audit popups: the check-in (checkin.html) and the end-of-day wrap-up
(wrapup.html), small HTML windows in the same house style as the control
window, with this file as their backend. Started by `screen_audit.py checkin`
and `screen_audit.py wrapup`; the check-in falls back to the old tkinter popup
if this one can't open.
"""

import datetime as dt
import json
import sys
import threading
from pathlib import Path

import webview

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import macos  # noqa: E402
import screen_audit as sa  # noqa: E402

MODES = {
    "checkin": {"title": "Screen Audit check-in", "page": "checkin.html", "size": (440, 470)},
    "wrapup": {"title": "Screen Audit wrap-up", "page": "wrapup.html", "size": (460, 600)},
}
MARGIN = 24


class Api:
    def __init__(self, cfg, record, shown):
        self._cfg, self._record, self._shown = cfg, record, shown
        self._window = None
        self._saved = False

    def info(self):
        return {"lang": self._cfg["language"], "question": self._record["question"], "window": self._record["window"][:90],
                "timeout_min": self._cfg["checkin_timeout_min"]}

    def finish(self, result, answer="", blocker=""):
        rec = self._record
        rec["result"] = result
        if result == "answered":
            rec["answer"] = (answer or "").strip() or None
            rec["blocker"] = (blocker or "").strip() or None
            if not rec["answer"] and not rec["blocker"]:
                rec["result"] = "skipped"
        if result == "pause":
            (self._cfg["data_dir"] / "paused_until").write_text(
                (sa.now() + dt.timedelta(hours=1)).isoformat(), encoding="utf-8")
        sa.append_jsonl(self._cfg["data_dir"] / "checkins" / f"{self._shown:%Y-%m-%d}.jsonl", rec)
        self._saved = True
        # let the page show its toast, then close
        threading.Timer(1.0 if result != "skipped" else 0, self._window.destroy).start()
        return {"ok": True}


class WrapupApi:
    """End of day: what the screen can't show. Saving (or leaving it untouched
    until it times out) writes today's log; "Not today" doesn't."""

    def __init__(self, cfg, shown):
        self._cfg, self._shown = cfg, shown
        self._day = f"{shown:%Y-%m-%d}"
        self._window = None
        self._saved = False

    def info(self):
        cfg, day = self._cfg, self._day
        checkins = sa.read_jsonl(cfg["data_dir"] / "checkins" / f"{day}.jsonl")
        return {"lang": cfg["language"], "active_min": round(sa.active_minutes(cfg, day)),
                "checkins": sum(1 for c in checkins if c.get("answer")),
                "blockers": [f"{c['t'][11:16]} {c['blocker']}" for c in checkins if c.get("blocker")][-3:],
                "timeout_min": cfg["checkin_timeout_min"] * 3}

    def finish(self, result, answers=None):
        self.save(result, answers)
        threading.Timer(1.2 if result != "dismissed" else 0, self._window.destroy).start()
        return {"ok": True}

    def save(self, result, answers=None):
        a = {k: (v or "").strip() for k, v in (answers or {}).items()}
        rec = {"t": sa.now().isoformat(), "result": result,
               "done": a.get("done") or None, "blockers": a.get("blockers") or None,
               "tomorrow": a.get("tomorrow") or None}
        if result == "answered" and not any((rec["done"], rec["blockers"], rec["tomorrow"])):
            rec["result"] = "empty"
        out = self._cfg["data_dir"] / "wrapups" / f"{self._day}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        if result != "dismissed":
            sa.spawn(self._cfg, "daily")
        self._saved = True


def place(size, centre=False):
    """Where the popup opens, as pywebview's (x, y): points from the top left
    of the screen in use. Check-ins sit bottom-right of its visible frame
    (clear of the menu bar and the Dock); the wrap-up is the end of the day,
    so it takes the middle. (None, None) lets pywebview centre it."""
    try:
        from AppKit import NSScreen
        screen = NSScreen.mainScreen()
        f, v = screen.frame(), screen.visibleFrame()
    except Exception:
        return None, None
    return popup_position((f.origin.x, f.origin.y, f.size.width, f.size.height),
                          (v.origin.x, v.origin.y, v.size.width, v.size.height), size, centre)


def popup_position(frame, visible, size, centre=False):
    """frame and visible are AppKit rectangles (x, y, w, h, origin bottom left)."""
    fx, fy, fw, fh = frame
    vx, vy, vw, vh = visible
    w, h = size
    left = vx - fx                     # visible area, measured from the screen's top left
    top = (fy + fh) - (vy + vh)
    if centre:
        return round(left + (vw - w) / 2), round(top + (vh - h) / 2)
    return round(left + vw - w - MARGIN), round(top + vh - h - MARGIN)


def main(mode="checkin"):
    cfg = sa.load_config()
    lock = sa.single_instance(cfg, mode)
    if not lock:
        return
    shown = sa.now()
    if mode == "wrapup":
        record = None
        api = WrapupApi(cfg, shown)
    else:
        question, context = sa.pick_question(cfg)
        _, title, proc, _ = sa.foreground()
        record = {"t": shown.isoformat(), "question": question, "window": title[:200], "proc": proc,
                  "context": context, "answer": None, "blocker": None, "result": "timeout"}
        api = Api(cfg, record, shown)
    m = MODES[mode]
    x, y = place(m["size"], centre=mode == "wrapup")
    win = webview.create_window(m["title"], url=str(HERE / m["page"]), js_api=api, x=x, y=y,
                                width=m["size"][0], height=m["size"][1], resizable=False, on_top=True,
                                background_color="#16171a" if macos.system_dark() else "#fbfbfa")
    api._window = win
    macos.app_icon()
    # On top of everything, but the keyboard stays with what they were typing
    # in until they click the popup (pywebview would otherwise take it).
    before = macos.frontmost_app()
    win.events.shown += lambda: threading.Timer(0.3, macos.give_focus_back, args=(before,)).start()
    webview.start(gui="cocoa")
    if not api._saved:  # window closed with the X
        if mode == "wrapup":
            api.save("dismissed")
        else:
            record["result"] = "skipped"
            sa.append_jsonl(cfg["data_dir"] / "checkins" / f"{shown:%Y-%m-%d}.jsonl", record)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "checkin")
