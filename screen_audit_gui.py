"""Screen Audit control window: an HTML page (ui.html) in a native window via
pywebview, with this file as its backend. Start, pause and stop recording, see
how far the audit has got, check in, write the report, change settings.

Closing the window does not stop recording; that runs as its own background
process (screen_audit.py run). Open the window again any time to check on it.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import macos  # noqa: E402
import screen_audit as sa  # noqa: E402

TITLE = "Screen Audit"
CONFIG = Path(os.environ.get("SCREEN_AUDIT_CONFIG", HERE / "config.json"))  # the same file load_config reads
system_dark = macos.system_dark


def save_config(changes):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    cfg.update(changes)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


class Totals:
    """Whole-audit totals. Past days don't change, so each file is re-read
    only when its modification time moves."""

    def __init__(self):
        self.cache = {}

    def _read(self, path, fold):
        try:
            m = path.stat().st_mtime
        except OSError:
            return None
        hit = self.cache.get(path)
        if hit and hit[0] == m:
            return hit[1]
        val = fold(sa.read_jsonl(path))
        self.cache[path] = (m, val)
        return val

    def get(self, cfg):
        data = cfg["data_dir"]
        days = sorted((data / "activity").glob("*.jsonl")) if (data / "activity").exists() else []
        active = sum(self._read(p, lambda rows: sum(1 for e in rows if e.get("state") in ("frame", "same"))) or 0
                     for p in days)
        blocks = sum(self._read(p, len) or 0 for p in (data / "analysis").glob("*.jsonl")) \
            if (data / "analysis").exists() else 0
        answered = sum(self._read(p, lambda rows: sum(1 for c in rows if c.get("answer"))) or 0
                       for p in (data / "checkins").glob("*.jsonl")) if (data / "checkins").exists() else 0
        return {"days": len(days), "first_day": days[0].stem if days else None,
                "hours": round(active * cfg["interval_sec"] / 3600, 1),
                "blocks": blocks, "checkins": answered}


CLAUDE_RECHECK_HOURS = 6


def _first_upper(text):
    return text[:1].upper() + text[1:]


class Api:
    def __init__(self):
        self._cfg = sa.load_config()
        self._totals = Totals()
        self._report = {"busy": False, "message": None, "ok": None}
        self._window = None
        self._claude = sa.read_state(self._cfg).get("claude_check") or {}
        self._claude_busy = False
        self._claude_again = False
        last = self._claude.get("at", "")
        stale = last < (dt.datetime.now() - dt.timedelta(hours=CLAUDE_RECHECK_HOURS)).isoformat()
        other_ai = self._claude.get("ai", "claude") != self._cfg["ai"]
        if not self._claude.get("ok") or stale or other_ai:  # a working login is trusted for a few hours
            self.check_claude()

    # ------------------------------------------------------------ read

    def status(self):
        s = sa.get_status(self._cfg)
        pu = s["paused_until"]
        state = "stopped" if not s["running"] else "paused" if pu else "recording"
        recent = [e for e in s["errors"] if dt.datetime.fromisoformat(e["t"]) > dt.datetime.now() - dt.timedelta(hours=1)]
        started = sa.read_state(self._cfg).get("run_started") if s["running"] else None
        return {
            "lang": self._cfg["language"],
            "state": state,
            "paused_forever": pu == dt.datetime.max if pu else False,
            "paused_until": pu.strftime("%H:%M") if pu and pu != dt.datetime.max else None,
            "since": started[11:16] if started else None,
            "interval": self._cfg["interval_sec"],
            "today": {"active_min": s["active_min"], "frames": s["frames"], "blocks": s["blocks"],
                      "checkins_answered": s["checkins_answered"], "checkins_asked": s["checkins_asked"],
                      "cost": round(s["cost_today"], 2)},
            "last_summary": s["last_summary"],
            "last_block_end": s["last_block_end"][11:16] if s["last_block_end"] else None,
            "audit": {**self._totals.get(self._cfg), "target": self._cfg["audit_days"]},
            "last_report": s["last_report"].name if s["last_report"] else None,
            "report": self._report,
            "daily": self._daily(s),
            "error": ({"at": recent[-1]["t"][11:16], "where": recent[-1]["where"], "text": recent[-1]["error"][:240]}
                      if recent else None),
            "permissions": self.permissions(),
            "ai": self._cfg["ai"],
            "claude": self._claude_status(),
        }

    # ------------------------------------------------------------ Claude

    def _claude_status(self):
        """The last check, for the AI chosen now. A result for the other AI
        (the setting changed while a check ran) counts as still checking."""
        ai = self._cfg["ai"]
        current = self._claude.get("ai", "claude") == ai
        shown = self._claude if current else {}
        return {**shown, "ai": ai, "checking": self._claude_busy or not current,
                "install": sa.CODEX_INSTALL if ai == "codex" else sa.CLAUDE_INSTALL}

    def check_claude(self):
        """Check the chosen AI's login in the background; the window polls status().
        Asked again while one runs (the AI was just changed), it runs once more after."""
        if self._claude_busy:
            self._claude_again = True
            return
        self._claude_busy = True

        def work():
            while True:
                self._claude_again = False
                cfg = self._cfg
                try:
                    result = sa.check_ai(cfg)
                except Exception as e:
                    result = {"ok": False, "kind": "error", "message": repr(e)[:300], "at": sa.now().isoformat(),
                              "ai": cfg["ai"]}
                self._claude = result
                sa.write_state(cfg, claude_check=result)
                if not self._claude_again and result.get("ai", "claude") == self._cfg["ai"]:
                    break
            self._claude_busy = False

        threading.Thread(target=work, daemon=True).start()

    def copy_text(self, text):
        macos.copy_text(str(text))

    def permissions(self):
        """What macOS allows this app. Screen Recording is needed for anything
        to be saved; Accessibility only fills in some window titles."""
        screen, ax = macos.screen_recording_granted(), macos.accessibility_granted()
        return {"screen": screen is not False, "accessibility": ax is not False,
                "dismissed": bool(self._cfg.get("permissions_dismissed"))}

    def open_permission(self, kind):
        if kind in macos.SETTINGS_PANES:
            macos.request_permission(kind)

    def dismiss_permissions(self):
        save_config({"permissions_dismissed": True})
        self._cfg = sa.load_config()
        return self.status()

    def _daily(self, s):
        """Today's log: written (and when), being written, or not yet."""
        job, today = s["daily_job"], f"{dt.date.today()}"
        mine = job.get("day") == today
        # a crashed writer leaves busy behind; after 20 minutes stop believing it
        busy = mine and job.get("busy") and job.get("started", "") > \
            (dt.datetime.now() - dt.timedelta(minutes=20)).isoformat()
        written = dt.datetime.fromtimestamp(s["daily_file"].stat().st_mtime).strftime("%H:%M") \
            if s["daily_file"] else None
        return {"written": written, "busy": bool(busy),
                "message": job.get("message") if mine and not busy and job.get("ok") is False else None,
                "wrapup_enabled": self._cfg["wrapup_enabled"], "wrapup_time": self._cfg["wrapup_time"],
                "wrapup_answered": s["wrapup_answered"]}

    def settings(self):
        c = self._cfg
        return {"language": c["language"], "ai": c["ai"], "checkin_per_day": c["checkin_per_day"], "wrapup_enabled": c["wrapup_enabled"],
                "wrapup_time": c["wrapup_time"], "interval_sec": c["interval_sec"],
                "keep_frames_days": c["keep_frames_days"], "audit_days": c["audit_days"],
                "startup": macos.start_at_login_enabled(), "privacy": "\n".join(c["exclude_title_keywords"]),
                "data_dir": str(c["data_dir"]), "frames_location": c["frames_location"],
                "frames": self.frames_preview(c["frames_location"])}

    def storage(self):
        """Space taken by screenshots, logs and reports, and what's left."""
        c = self._cfg
        roots = [c["frames_dir"]] + ([c["data_dir"] / "frames"] if c["data_dir"] / "frames" != c["frames_dir"] else [])
        days = set()
        for root in roots:
            if root.exists():
                for d in root.iterdir():
                    try:
                        dt.datetime.strptime(d.name, "%Y-%m-%d")
                        days.add(d.name)
                    except ValueError:
                        pass
        frames = sum(macos.dir_size(r) for r in roots)
        data = macos.dir_size(c["data_dir"], skip=[r for r in roots if r.is_relative_to(c["data_dir"])])
        probe = c["frames_dir"]
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        # days past the keep setting that are still here because they aren't summarised yet
        done = sa.read_state(c).get("analyzed_until") or ""
        cutoff = f"{dt.date.today() - dt.timedelta(days=c['keep_frames_days'])}"
        waiting = sorted(d for d in days if d < cutoff and d >= done[:10])
        return {"frames": frames, "days": len(days), "first": min(days) if days else None,
                "last": max(days) if days else None, "data": data, "reports": macos.dir_size(HERE / "reports"),
                "free": free, "keep_days": c["keep_frames_days"], "waiting_days": len(waiting),
                "frames_dir": str(c["frames_dir"])}

    def frames_preview(self, location):
        """Where frames would go for this location, and the space left there."""
        d = sa.frames_dir_for(self._cfg["data_dir"], location)
        # drive: the volume's name, or None for the startup disk
        out = {"dir": str(d), "default": not (location or "").strip(), "free_gb": None, "drive": macos.volume_name(d)}
        probe = d
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            out["free_gb"] = round(shutil.disk_usage(probe).free / 1e9, 1)
        except OSError:
            out["error"] = sa.tr(self._cfg, "err_drive")
        return out

    def choose_folder(self):
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        picked = self._window.create_file_dialog(kind, directory=self._cfg["frames_location"] or str(self._cfg["data_dir"]))
        return picked[0] if picked else None

    # ------------------------------------------------------------ recording

    def _spawn(self, *args):
        sa.spawn(self._cfg, *args)

    def start(self):
        if not sa.is_running(self._cfg):
            self._spawn("run")
            for _ in range(30):
                time.sleep(0.2)
                if sa.is_running(self._cfg):
                    break
        return self.status()

    def stop(self):
        sa.stop_recorder(self._cfg)
        return self.status()

    def pause(self, minutes):
        sa.cmd_pause(self._cfg, argparse.Namespace(minutes=int(minutes)))
        return self.status()

    def resume(self):
        sa.cmd_resume(self._cfg, None)
        return self.status()

    # ------------------------------------------------------------ actions

    def checkin(self):
        self._spawn("checkin")

    def summarise(self):
        self._spawn("analyze")

    def open_frames(self):
        self._cfg["frames_dir"].mkdir(parents=True, exist_ok=True)
        macos.open_path(self._cfg["frames_dir"])

    def open_data(self):
        self._cfg["data_dir"].mkdir(parents=True, exist_ok=True)
        macos.open_path(self._cfg["data_dir"])

    def open_context(self):
        macos.open_path(sa.context_file())

    def write_daily(self):
        """The wrap-up questions first, unless they're off or already answered today."""
        d = self._daily(sa.get_status(self._cfg))
        if d["busy"]:
            return "busy"
        if self._cfg["wrapup_enabled"] and not d["wrapup_answered"]:
            self._spawn("wrapup")
            return "wrapup"
        self._spawn("daily")
        return "writing"

    def open_daily(self):
        p = sa.daily_path(f"{dt.date.today()}")
        if p.exists():
            macos.open_path(p)

    def open_report(self):
        last = sa.get_status(self._cfg)["last_report"]
        if last:
            macos.open_path(last)

    # ------------------------------------------------------------ history

    def history(self):
        """Days that have 15-minute summaries (newest first) and the written
        logs and reports (newest first), for the History page."""
        days = []
        for f in sorted((self._cfg["data_dir"] / "analysis").glob("*.jsonl"), reverse=True):
            blocks = [b for b in sa.read_jsonl(f) if b.get("summary")]
            if blocks:
                days.append({"day": f.stem, "blocks": len(blocks),
                             "active_min": round(sum(b.get("active_min") or 0 for b in blocks))})
        docs = []
        root = HERE / "reports"
        for sub, kind in (("daily", "daily"), (".", "report")):
            for f in sorted((root / sub).glob("*.md"), reverse=True):
                docs.append({"kind": kind, "key": f.relative_to(root).as_posix(),
                             "title": f.stem.replace("report-", ""),
                             "written": dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
        return {"days": days, "docs": docs}

    def history_day(self, day):
        """One day's blocks, oldest first."""
        f = self._cfg["data_dir"] / "analysis" / f"{day}.jsonl"
        if f.stem != day or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return []
        return [{"start": b["start"][11:16], "end": b["end"][11:16], "active_min": round(b.get("active_min") or 0),
                 "summary": b["summary"], "question": b.get("question") or "",
                 "activities": [{"text": a.get("activity", ""), "minutes": round(a.get("minutes") or 0),
                                 "category": a.get("category", ""), "process": a.get("process", ""),
                                 "tools": a.get("tools") or []} for a in b.get("activities") or []]}
                for b in sa.read_jsonl(f) if b.get("summary")]

    def _doc_path(self, key):
        """A report file inside reports/, nothing else."""
        root = (HERE / "reports").resolve()
        p = (root / str(key)).resolve()
        return p if p.suffix == ".md" and p.is_relative_to(root) and p.is_file() else None

    def read_doc(self, key):
        p = self._doc_path(key)
        return {"text": p.read_text(encoding="utf-8")} if p else {"text": None}

    def open_doc(self, key):
        p = self._doc_path(key)
        if p:
            macos.open_path(p)

    def write_report(self, days):
        if self._report["busy"]:
            return
        self._report = {"busy": True, "message": sa.tr(self._cfg, "report_busy", ai=_first_upper(sa.model_label(self._cfg, "report"))), "ok": None}

        def work():
            try:
                subprocess.run([sys.executable, str(HERE / "screen_audit.py"), "analyze"],
                               cwd=str(HERE), timeout=3600, capture_output=True)
                out = sa.cmd_report(self._cfg, argparse.Namespace(days=int(days), model=None))
                if out:
                    self._report = {"busy": False, "message": sa.tr(self._cfg, "report_done", name=out.name), "ok": True}
                    macos.open_path(out)
                else:
                    self._report = {"busy": False, "ok": False, "message": sa.tr(self._cfg, "report_empty")}
            except Exception as e:
                self._report = {"busy": False, "message": sa.tr(self._cfg, "report_failed", e=e), "ok": False}

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ settings

    def save_settings(self, s):
        location = (s.get("frames_location") or "").strip()
        target = sa.frames_dir_for(self._cfg["data_dir"], location)
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            return {"error": sa.tr(self._cfg, "err_folder", target=target, e=e.strerror or e)}
        words = [w.strip() for w in s["privacy"].splitlines() if w.strip()]
        wrapup_time = s.get("wrapup_time") or self._cfg["wrapup_time"]
        try:
            dt.datetime.strptime(wrapup_time, "%H:%M")
        except ValueError:
            return {"error": sa.tr(self._cfg, "err_time", t=wrapup_time)}
        save_config({"language": s["language"] if s.get("language") in sa.LANGUAGES else self._cfg["language"],
                     "ai": s["ai"] if s.get("ai") in sa.AIS else self._cfg["ai"],
                     "checkin_per_day": int(s["checkin_per_day"]), "interval_sec": int(s["interval_sec"]),
                     "wrapup_enabled": bool(s["wrapup_enabled"]), "wrapup_time": wrapup_time,
                     "keep_frames_days": int(s["keep_frames_days"]), "audit_days": int(s["audit_days"]),
                     "exclude_title_keywords": words, "frames_location": location})
        if bool(s["startup"]) != macos.start_at_login_enabled():
            macos.set_start_at_login(bool(s["startup"]), log=sa.background_log(self._cfg))
        ai_changed = self._cfg["ai"] != sa.load_config()["ai"]
        self._cfg = sa.load_config()
        if ai_changed:
            self.check_claude()
        restarted = False
        if sa.is_running(self._cfg):
            self.stop()
            self.start()
            restarted = True
        return {"restarted": restarted, "frames_dir": str(target)}


def show_request_file(cfg):
    """A second launch asks the open window to come forward by creating this."""
    return cfg["data_dir"] / "show_request"


def watch_show_requests(cfg, win):
    f = show_request_file(cfg)
    while True:
        time.sleep(0.5)
        if f.exists():
            f.unlink(missing_ok=True)
            try:
                win.restore()
                win.show()
                macos.bring_to_front()
            except Exception:
                pass


def main():
    cfg = sa.load_config()
    lock = sa.single_instance(cfg, "gui")
    if not lock:
        show_request_file(cfg).touch()
        return
    show_request_file(cfg).unlink(missing_ok=True)
    api = Api()
    win = webview.create_window(TITLE, url=str(HERE / "ui.html"), js_api=api, width=440, height=860,
                                min_size=(400, 560), background_color="#16171a" if system_dark() else "#fbfbfa")
    api._window = win
    macos.app_icon()
    threading.Thread(target=watch_show_requests, args=(cfg, win), daemon=True).start()
    webview.start(gui="cocoa", debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
