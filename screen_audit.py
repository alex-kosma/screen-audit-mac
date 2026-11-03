"""Screen audit: watch what you actually work on for a week, then ask Claude
what in it could be automated.

How: a screenshot every few seconds, a short Claude summary of each
15-minute block, a handful of "what are you doing and why" check-ins a day,
and a weekly report that turns it all into a task inventory with an
automation verdict per task.

Everything stays on this machine except the screenshots and logs sent to
Claude through your own `claude` CLI login for the summaries.

The everyday way in is the window, screen_audit_gui.py (the "Screen Audit"
app that make_icon.py puts in ~/Applications). These commands are what it
runs underneath.

    python screen_audit.py run [--no-tray]  capture loop with its menu bar icon (the window starts it in the background)
    python screen_audit.py analyze          summarise finished 15-min blocks now
    python screen_audit.py checkin          pop a check-in question now
    python screen_audit.py wrapup           end-of-day questions, then write today's log
    python screen_audit.py daily [--date D] write the daily log for today (or D)
    python screen_audit.py report [--days 7] write the task-inventory report
    python screen_audit.py pause [minutes]  stop capturing (default 60, 0 = until resume)
    python screen_audit.py resume
    python screen_audit.py status
    python screen_audit.py stop             stop the recorder
    python screen_audit.py restart          stop the recorder and start a fresh one
    python screen_audit.py doctor [--try]   check the whole setup; --try also plays the sounds
                                            and shows a test notification (see AGENTS.md)
"""

import argparse
import copy
import datetime as dt
import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import macos  # noqa: E402

macos.extend_path()

DEFAULTS = {
    "data_dir": str(HERE / "data"),  # screenshots, logs, summaries
    "interval_sec": 5,
    # 0 keeps the screen's own resolution. 1600 at quality 55 (the first
    # setting) made small text unreadable on a 2560px screen.
    "max_width": 0,
    "jpeg_quality": 85,
    # 0 = no chroma subsampling: keeps coloured text and thin UI lines sharp.
    "jpeg_subsampling": 0,
    # Mean absolute difference (0-255) on a 64x36 greyscale thumbnail below
    # which a frame counts as unchanged and is not saved again.
    "dedupe_threshold": 1.5,
    # Also capture the monitors the active window is not on, saved as separate
    # "side" frames so each screen keeps its own resolution for the model.
    "capture_all_screens": True,
    "side_frames_per_batch": 3,
    "idle_skip_sec": 120,
    "sounds": True,  # a short tone when recording is paused (falling) and resumed (rising)
    "exclude_title_keywords": [
        "password", "1password", "bitwarden", "keepass", "lastpass",
        "inprivate", "incognito", "private browsing",
        "bank", "otp", "revolut", "wise.com", "paypal", "ügyfélkapu",
        "sign in", "log in", "login",
    ],
    # App names as macOS shows them in the menu bar, case ignored. macOS
    # translates its own apps' names (Passwords is "Паролі" in Ukrainian), so
    # exclude_bundle_ids below is what reliably catches those.
    "exclude_processes": ["Keychain Access", "Passwords", "1Password", "1Password 7", "1Password 8",
                          "Bitwarden", "KeePassXC", "Strongbox"],
    # The same apps by bundle id, which doesn't change with the language.
    "exclude_bundle_ids": ["com.apple.keychainaccess", "com.apple.Passwords", "com.1password.1password",
                           "com.agilebits.onepassword7", "com.bitwarden.desktop", "org.keepassxc.keepassxc",
                           "com.markmcguill.strongbox.mac"],
    "batch_minutes": 15,
    "frames_per_batch": 6,
    "analysis_model": "haiku",
    "report_model": "opus",
    "report_effort": "max",
    "keep_frames_days": 3,
    # Where screenshots go. Empty = data/frames. Otherwise the app creates its
    # own "Screen Audit frames" folder inside this one and only ever cleans
    # inside that, so it can't touch your own date-named folders.
    "frames_location": "",
    # About 8 a day in practice: 45-100 min apart over working hours. At 12
    # (the first setting) 7 of 8 still got answered, but the cap was never hit.
    "checkin_per_day": 8,
    "checkin_min_gap_min": 45,
    "checkin_hours": [9, 22],
    "checkin_timeout_min": 5,
    # End-of-day wrap-up: a popup at this time asking what the screen can't
    # show, then today's log is written. Only on days with some active time.
    "wrapup_enabled": True,
    "wrapup_time": "18:30",
    "wrapup_min_active_min": 30,
    "daily_model": "sonnet",
    # How many recorded days the window counts toward before suggesting the report.
    "audit_days": 7,
    # "en" or "uk": the windows, the menu bar icon, and what Claude writes. Picked
    # from the macOS preferred language on first launch, then saved to config.json.
    "language": "",
    # Full path to the claude CLI if it isn't found on its own.
    "claude_path": "",
    # Who writes the summaries, daily logs and report: "claude" (Claude Code,
    # on a Claude plan or Anthropic Console account) or "codex" (OpenAI's Codex
    # CLI, on a ChatGPT plan or OpenAI API key). Screenshots go to that company.
    "ai": "claude",
    "codex_path": "",
    # Codex model per job; empty = Codex's own default. The summaries run 96
    # times a day with screenshots, so they get the small, fast model to keep
    # a ChatGPT plan's Codex allowance for the daily log and the report.
    "codex_models": {"analysis": "gpt-6-luna", "daily": "", "report": ""},
    # How hard Codex thinks per job.
    "codex_effort": {"analysis": "low", "daily": "medium", "report": "high"},
}

LANGUAGES = ("en", "uk")
AIS = ("claude", "codex")


# ---------------------------------------------------------------- config

def load_config():
    cfg = copy.deepcopy(DEFAULTS)  # nothing a caller changes leaks into the defaults
    path = Path(os.environ.get("SCREEN_AUDIT_CONFIG", HERE / "config.json"))
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if user.get("language") not in LANGUAGES:  # first launch: follow the OS, then keep it
        user["language"] = detect_language()
        path.write_text(json.dumps(user, indent=2, ensure_ascii=False), encoding="utf-8")
    for key, value in user.items():
        if isinstance(cfg.get(key), dict):  # per-job settings: a partial override keeps the other jobs
            if isinstance(value, dict):
                cfg[key].update(value)
        else:
            cfg[key] = value
    cfg["data_dir"] = Path(os.path.expandvars(cfg["data_dir"])).resolve()
    cfg["frames_dir"] = frames_dir_for(cfg["data_dir"], cfg["frames_location"])
    return cfg


def detect_language():
    """"uk" when the first macOS preferred language is Ukrainian, otherwise "en"."""
    name = macos.preferred_language().lower()
    if not name:
        import locale
        name = (locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
    return "uk" if name.startswith(("uk", "ukrainian")) else "en"


# Words the Python side shows: tray, popups' stats, headers of written files,
# messages the window relays. The pages have their own list in i18n.js.
TEXT = {
    "en": {
        "tray_open": "Open Screen Audit", "tray_pause15": "Pause 15 min", "tray_pause60": "Pause 1 hour",
        "tray_resume": "Resume", "tray_stop": "Exit (stops recording)", "tray_restart": "Restart",
        "tray_recording": "Screen Audit: recording", "tray_paused": "Screen Audit: paused",
        "tray_paused_until": "Screen Audit: paused until {t}",
        "tray_problem_title": "Screen Audit: summaries are failing",
        "tray_problem_login": "{ai} is signed out or out of credit. Recording continues, but nothing is "
                              "summarised. Run `{fix}` in a terminal.",
        "tray_problem_other": "{ai} failed: {text}. Recording continues. Details in the window.",
        "daily_title": "Work log, {weekday} {day}",
        "daily_meta": "{h}h {m:02d}m active, {blocks} blocks, {checkins} check-ins{wrapup}. Written {at} with {model}.",
        "daily_wrapup": ", wrap-up answered",
        "daily_failed": "The log failed: {e}",
        "daily_empty": "Nothing to log yet. No summarised blocks or answers for that day.",
        "report_title": "Screen audit report, {start} to {end}",
        "report_meta": "{blocks} analysed blocks, {checkins} check-in answers, {hours} active hours. Generated {at} with {model}.",
        "report_busy": "Summarising what's left, then writing the report with {ai}. This takes a few minutes.",
        "report_done": "Report written: {name}",
        "report_empty": "Nothing to report yet. No summarised blocks in that range.",
        "report_failed": "The report failed: {e}",
        "err_folder": "Couldn't create or write to {target}: {e}",
        "err_time": "Wrap-up time should look like 18:30, not {t}.",
        "err_drive": "That drive isn't available.",
        "err_screen_recording": "Screen Recording is not allowed, so no screenshots are saved. Turn it on in "
                                "System Settings, Privacy & Security, Screen & System Audio Recording, then stop "
                                "and start recording.",
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    },
    "uk": {
        "tray_open": "Відкрити Screen Audit", "tray_pause15": "Пауза 15 хв", "tray_pause60": "Пауза 1 година",
        "tray_resume": "Продовжити", "tray_stop": "Вийти (зупиняє запис)", "tray_restart": "Перезапустити",
        "tray_recording": "Screen Audit: запис", "tray_paused": "Screen Audit: пауза",
        "tray_paused_until": "Screen Audit: пауза до {t}",
        "tray_problem_title": "Screen Audit: підсумки не працюють",
        "tray_problem_login": "{ai} розлогінений або закінчився ліміт. Запис триває, але нічого не "
                              "підсумовується. Виконайте `{fix}` у терміналі.",
        "tray_problem_other": "Помилка {ai}: {text}. Запис триває. Деталі у вікні.",
        "daily_title": "Журнал роботи, {weekday} {day}",
        "daily_meta": "Активно {h} год {m:02d} хв, блоків: {blocks}, чек-інів: {checkins}{wrapup}. Написано о {at}, модель {model}.",
        "daily_wrapup": ", є відповіді на підсумок дня",
        "daily_failed": "Журнал не вдався: {e}",
        "daily_empty": "Поки нема що записати: за цей день немає підсумків чи відповідей.",
        "report_title": "Звіт аудиту екрана, {start} – {end}",
        "report_meta": "Блоків: {blocks}, відповідей на чек-іни: {checkins}, активних годин: {hours}. Створено {at}, модель {model}.",
        "report_busy": "Підсумовую решту, потім {ai} пише звіт. Це займе кілька хвилин.",
        "report_done": "Звіт готовий: {name}",
        "report_empty": "Поки нема про що звітувати: у цьому періоді немає підсумованих блоків.",
        "report_failed": "Звіт не вдався: {e}",
        "err_folder": "Не вдалося створити папку {target} або записати в неї: {e}",
        "err_time": "Час підсумку має виглядати як 18:30, а не {t}.",
        "err_drive": "Цей диск недоступний.",
        "err_screen_recording": "Запис екрана не дозволено, тому знімки не зберігаються. Увімкніть його в "
                                "Системних параметрах: Приватність і безпека, Запис екрана й системного аудіо, "
                                "потім зупиніть і почніть запис знову.",
        "weekdays": ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"],
    },
}

# Added to prompts when the language isn't English. In the 15-minute summaries,
# keys, categories and process names stay English so blocks stay comparable
# across days; the documents people read are Ukrainian throughout.
LANG_RULE = {
    "en": {"analysis": "", "doc": ""},
    "uk": {
        "analysis": "Language: write summary, activity, trigger, automation_note and question in Ukrainian. "
                    "Keep JSON keys, category values and process names in English.",
        "doc": "Language: write the whole document in Ukrainian, every section heading included (for the daily log: "
               "Коротко, Зроблено, Куди пішов час, Перешкоди, Незавершене, Завтра). Translate English process and "
               "category names from the data into natural Ukrainian; product and app names stay as they are.",
    },
}


def tr(cfg, key, **kw):
    s = TEXT.get(cfg["language"], TEXT["en"]).get(key, TEXT["en"][key])
    return s.format(**kw) if kw else s


def lang_rule(cfg, kind="doc"):
    rule = LANG_RULE.get(cfg["language"], LANG_RULE["en"])[kind]
    return "\n\n" + rule if rule else ""


FRAMES_SUBFOLDER = "Screen Audit frames"


def frames_dir_for(data_dir, location):
    loc = (location or "").strip()
    return (Path(os.path.expandvars(loc)).resolve() / FRAMES_SUBFOLDER) if loc else data_dir / "frames"


def frame_path(cfg, frame):
    """A frame as logged: absolute since frames_location exists, relative to
    data_dir before that ("frames/<day>/<time>.jpg")."""
    p = Path(frame)
    return p if p.is_absolute() else cfg["data_dir"] / p


def now():
    return dt.datetime.now().replace(microsecond=0)


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a half-written last line from a crash
    return out


def context_file():
    """The person's list of what's already automated. It's theirs, so git leaves it
    out; it starts as a copy of context.example.md."""
    p = HERE / "context.md"
    if not p.exists():
        shutil.copyfile(HERE / "context.example.md", p)
    return p


def read_state(cfg):
    p = cfg["data_dir"] / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_state(cfg, **changes):
    state = read_state(cfg)
    state.update(changes)
    p = cfg["data_dir"] / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- macOS

idle_seconds = macos.idle_seconds
foreground = macos.foreground  # (bounds, title, app name, bundle id)


# ---------------------------------------------------------------- capture

def is_excluded(cfg, title, proc, bundle=""):
    t = title.lower()
    if any(k.lower() in t for k in cfg["exclude_title_keywords"]):
        return True
    if bundle and bundle.lower() in (b.lower() for b in cfg.get("exclude_bundle_ids", [])):
        return True
    return proc.lower() in (p.lower() for p in cfg["exclude_processes"])


def paused_until(cfg):
    p = cfg["data_dir"] / "paused_until"
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if raw == "forever":
        return dt.datetime.max
    until = dt.datetime.fromisoformat(raw)
    if until <= now():
        p.unlink(missing_ok=True)
        return None
    return until


class Capturer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_thumb = None
        self.side_last = {}  # display rect -> (thumb, saved path)
        self.permission_reported = 0

    def save(self, img, out):
        from PIL import Image
        cfg = self.cfg
        if cfg["max_width"] and img.width > cfg["max_width"]:
            img = img.resize((cfg["max_width"], round(img.height * cfg["max_width"] / img.width)), Image.LANCZOS)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(out, quality=cfg["jpeg_quality"], subsampling=cfg["jpeg_subsampling"], optimize=True)

    def side_frames(self, shots, active_rect, t, day):
        """The other displays: a new file when one changed, else its last one."""
        from PIL import ImageChops, ImageStat
        paths = []
        for i, (rect, img) in enumerate(shots.items(), 1):
            if rect == active_rect:
                continue
            thumb = img.convert("L").resize((64, 36))
            last = self.side_last.get(rect)
            if last and ImageStat.Stat(ImageChops.difference(thumb, last[0])).mean[0] < self.cfg["dedupe_threshold"]:
                paths.append(last[1])
                continue
            out = self.cfg["frames_dir"] / day / f"{t.strftime('%H%M%S')}-side{i}.jpg"
            self.save(img, out)
            self.side_last[rect] = (thumb, str(out))
            paths.append(str(out))
        return paths

    def tick(self):
        from PIL import ImageChops, ImageStat

        cfg, t = self.cfg, now()
        day = t.strftime("%Y-%m-%d")
        log = cfg["data_dir"] / "activity" / f"{day}.jsonl"
        entry = {"t": t.isoformat()}

        if paused_until(cfg):
            entry["state"] = "paused"
        elif macos.screen_locked():
            entry["state"] = "locked"
        elif idle_seconds() > cfg["idle_skip_sec"]:
            entry["state"] = "idle"
        elif macos.screen_recording_granted() is False:
            # Without the permission macOS hands back only the wallpaper.
            entry["state"] = "no_permission"
            self.report_permission()
        else:
            bounds, title, proc, bundle = foreground()
            if is_excluded(cfg, title, proc, bundle):
                entry.update(state="excluded", proc=proc)
            else:
                entry.update(title=title, proc=proc)
                try:
                    rects = macos.display_rects()
                    rect = macos.display_for(bounds, rects)
                    shots = macos.grab_all(rects if cfg["capture_all_screens"] else [rect])
                    img = shots[rect]
                    if cfg["capture_all_screens"]:
                        side = self.side_frames(shots, rect, t, day)
                        if side:
                            entry["side"] = side
                except (OSError, subprocess.SubprocessError):
                    entry["state"] = "locked"  # the screen went away mid-grab (sleep, lock)
                    append_jsonl(log, entry)
                    return
                thumb = img.convert("L").resize((64, 36))
                if self.last_thumb is not None:
                    diff = ImageStat.Stat(ImageChops.difference(thumb, self.last_thumb)).mean[0]
                else:
                    diff = 255
                if diff < cfg["dedupe_threshold"]:
                    entry["state"] = "same"
                else:
                    self.last_thumb = thumb
                    out = cfg["frames_dir"] / day / f"{t.strftime('%H%M%S')}.jpg"
                    self.save(img, out)
                    entry.update(state="frame", frame=str(out))
        append_jsonl(log, entry)

    def report_permission(self):
        """One line in errors.jsonl an hour, so the window shows why nothing is saved."""
        if self.permission_reported and time.monotonic() - self.permission_reported < 3600:
            return
        self.permission_reported = time.monotonic()
        append_jsonl(self.cfg["data_dir"] / "errors.jsonl", {
            "t": now().isoformat(), "where": "permission", "error": tr(self.cfg, "err_screen_recording")})


def background_log(cfg):
    """Where background processes' output goes."""
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    return cfg["data_dir"] / "background.log"


def spawn(cfg, *args):
    """Run this script again in the background, in its own session."""
    macos.spawn("screen_audit.py", *args, log=background_log(cfg))


def cleanup_frames(cfg):
    """Delete screenshot folders older than keep_frames_days, but only once analysed."""
    done = read_state(cfg).get("analyzed_until")
    if not done:
        return
    cutoff = min(now() - dt.timedelta(days=cfg["keep_frames_days"]),
                 dt.datetime.fromisoformat(done))
    # The current folder plus the default one, so frames from before a folder
    # change still age out. Only date-named folders are ever touched.
    dirs = [d for root in {cfg["frames_dir"], cfg["data_dir"] / "frames"} if root.exists() for d in root.glob("*")]
    for d in dirs:
        try:
            if dt.datetime.strptime(d.name, "%Y-%m-%d") + dt.timedelta(days=1) <= cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except ValueError:
            pass


def next_checkin_time(cfg):
    gap = cfg["checkin_min_gap_min"]
    return now() + dt.timedelta(minutes=random.uniform(gap, gap * 2.2))


def checkin_due(cfg, at):
    t = now()
    lo, hi = cfg["checkin_hours"]
    if not (lo <= t.hour < hi) or t < at or paused_until(cfg):
        return False
    if idle_seconds() > 60:
        return False
    today = read_jsonl(cfg["data_dir"] / "checkins" / f"{t:%Y-%m-%d}.jsonl")
    return len(today) < cfg["checkin_per_day"]


def active_minutes(cfg, day):
    rows = read_jsonl(cfg["data_dir"] / "activity" / f"{day}.jsonl")
    return sum(1 for e in rows if e.get("state") in ("frame", "same")) * cfg["interval_sec"] / 60


def wrapup_due(cfg):
    """Once a day, from wrapup_time until midnight, the first moment the person
    is at the keyboard, not paused, and has done some work today."""
    if not cfg["wrapup_enabled"]:
        return False
    t = now()
    day = f"{t:%Y-%m-%d}"
    hh, mm = (int(x) for x in cfg["wrapup_time"].split(":"))
    if t < t.replace(hour=hh, minute=mm, second=0) or paused_until(cfg) or idle_seconds() > 60:
        return False
    if read_state(cfg).get("wrapup_offered") == day or (cfg["data_dir"] / "wrapups" / f"{day}.json").exists():
        return False
    return active_minutes(cfg, day) >= cfg["wrapup_min_active_min"]


def single_instance(cfg, name):
    """Hold an exclusive lock for the life of the process; None if taken."""
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    f = open(cfg["data_dir"] / f"{name}.lock", "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def stop_request_file(cfg):
    """The window asks a running recorder to stop by creating this file."""
    return cfg["data_dir"] / "stop_request"


def restart_request_file(cfg):
    """`restart` asks a running recorder to stop and start a fresh one with this file."""
    return cfg["data_dir"] / "restart_request"


def stop_recorder(cfg):
    """Ask the recorder to stop (it removes its menu bar icon on the way out);
    force it only if it hasn't gone within 3 seconds."""
    if not is_running(cfg):
        return
    stop_request_file(cfg).touch()
    for _ in range(20):
        time.sleep(0.15)
        if not is_running(cfg):
            return
    pid = read_state(cfg).get("run_pid")
    if pid:
        macos.kill(pid)
        time.sleep(0.5)
    stop_request_file(cfg).unlink(missing_ok=True)


CUES = {"pause": (659, 494, 330), "resume": (330, 494, 659, 880)}  # falling = going quiet, rising = back on


def cue_wav(freqs, note=0.085, rate=22050):
    """Soft sine notes with a quick attack and a decay, as WAV bytes. Our own tones, so they
    don't sound like a macOS system sound."""
    import io
    import math
    import struct
    import wave
    n, frames = int(rate * note), bytearray()
    for f in freqs:
        for i in range(n):
            env = min(1, i / 120) * (1 - i / n) ** 1.5
            frames += struct.pack("<h", int(11000 * env * math.sin(2 * math.pi * f * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def play_cue(name):
    threading.Thread(target=_quiet, args=(macos.play_wav, cue_wav(CUES[name])), daemon=True).start()


ALERT_EVERY_H = 3
AI_NAMES = {"claude": ("Claude", "claude /login"), "codex": ("Codex", "codex login")}


def problem_alert(cfg, alerted):
    """(title, text) for a notification when summaries fail, so it isn't found days later;
    None when there's nothing new. The same kind repeats at most every ALERT_EVERY_H hours.
    alerted holds "since" (errors from before this run were already seen) and "last"."""
    rows = [e for e in read_jsonl(cfg["data_dir"] / "errors.jsonl")[-5:]
            if e.get("where") in ("analyze", "daily", "wrapup") and e["t"] > alerted["since"]]
    if not rows:
        return None
    text = str(rows[-1]["error"])
    kind = "login" if LOGIN_PROBLEM.search(text) else "other"
    last = alerted["last"]
    if last and last[0] == kind and last[1] > (now() - dt.timedelta(hours=ALERT_EVERY_H)).isoformat():
        return None
    alerted["last"] = (kind, now().isoformat())
    ai, fix = AI_NAMES.get(cfg.get("ai"), AI_NAMES["claude"])
    msg = tr(cfg, "tray_problem_login", ai=ai, fix=fix) if kind == "login" else \
        tr(cfg, "tray_problem_other", ai=ai, text=text.split(": ", 1)[-1][:150])
    return tr(cfg, "tray_problem_title"), msg


def make_tray(cfg, stop_event, restart_event):
    """Menu bar icon for the life of the recorder: the menu opens the window,
    pauses, resumes, restarts and exits. Paused: amber light, eyes closed. The
    caller runs it on the main thread, which AppKit requires."""
    import pystray
    import make_icon

    images = {"rec": make_icon.draw(make_icon.RED).resize((64, 64)),
              "paused": make_icon.draw(make_icon.AMBER, sleeping=True).resize((64, 64))}

    def open_window(icon=None, item=None):
        macos.spawn("screen_audit_gui.py", log=background_log(cfg))

    def pause(minutes):
        return lambda icon, item: (cmd_pause(cfg, argparse.Namespace(minutes=minutes)), refresh(icon))

    def resume(icon, item):
        cmd_resume(cfg, None)
        refresh(icon)

    def stop(icon, item):
        stop_event.set()

    def restart(icon, item):  # cmd_run starts a fresh recorder once this one has let go
        restart_event.set()
        stop_event.set()

    shown = [object()]  # the paused_until the icon shows now; nothing yet
    was_paused = [bool(paused_until(cfg))]

    def refresh(icon):
        pu = paused_until(cfg)
        shown[0] = pu
        if bool(pu) != was_paused[0]:  # menu, window, check-in popup and the pause running out all land here
            was_paused[0] = bool(pu)
            if cfg.get("sounds", True):
                play_cue("pause" if pu else "resume")
        icon.icon = images["paused" if pu else "rec"]
        icon.title = (tr(cfg, "tray_paused") if pu == dt.datetime.max else tr(cfg, "tray_paused_until", t=f"{pu:%H:%M}")) \
            if pu else tr(cfg, "tray_recording")
        icon.update_menu()

    paused = lambda item: bool(paused_until(cfg))  # noqa: E731
    menu = pystray.Menu(
        pystray.MenuItem(tr(cfg, "tray_open"), open_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tr(cfg, "tray_pause15"), pause(15), visible=lambda i: not paused(i)),
        pystray.MenuItem(tr(cfg, "tray_pause60"), pause(60), visible=lambda i: not paused(i)),
        pystray.MenuItem(tr(cfg, "tray_resume"), resume, visible=paused),
        pystray.MenuItem(tr(cfg, "tray_restart"), restart),
        pystray.MenuItem(tr(cfg, "tray_stop"), stop),
    )
    icon = pystray.Icon("screen-audit", images["rec"], tr(cfg, "tray_recording"), menu)

    def watch():  # pause/resume can also come from the window or the check-in popup
        while not stop_event.is_set():
            if paused_until(cfg) != shown[0]:  # only touch AppKit when something changed
                macos.on_main_thread(lambda: _quiet(refresh, icon))
            stop_event.wait(2)

    alerted = {"since": now().isoformat(), "last": None}

    def watch_errors():
        while not stop_event.is_set():
            try:
                found = problem_alert(cfg, alerted)
                if found:
                    macos.notify(*found)
            except Exception:
                pass
            stop_event.wait(30)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=watch, daemon=True).start()
        threading.Thread(target=watch_errors, daemon=True).start()

    return icon, setup


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def log_error(cfg, where, e):
    append_jsonl(cfg["data_dir"] / "errors.jsonl", {"t": now().isoformat(), "where": where, "error": repr(e)[:800]})


def capture_loop(cfg, stop_event, restart_event=None):
    cap = Capturer(cfg)
    next_analyze = now() + dt.timedelta(minutes=cfg["batch_minutes"])
    next_cleanup = now()
    next_checkin = next_checkin_time(cfg)
    next_wrapup_check = now()
    stop_file, restart_file = stop_request_file(cfg), restart_request_file(cfg)
    print(f"Capturing every {cfg['interval_sec']}s into {cfg['data_dir']} (Ctrl+C to stop)")
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                cap.tick()
            except Exception as e:  # never let one bad frame kill a week-long run
                log_error(cfg, "capture", e)
            t = now()
            if t >= next_analyze:
                spawn(cfg, "analyze")
                next_analyze = t + dt.timedelta(minutes=cfg["batch_minutes"])
            if t >= next_wrapup_check:
                next_wrapup_check = t + dt.timedelta(minutes=1)
                if wrapup_due(cfg):
                    write_state(cfg, wrapup_offered=f"{t:%Y-%m-%d}")
                    spawn(cfg, "wrapup")
                    next_checkin = max(next_checkin, t + dt.timedelta(minutes=cfg["checkin_min_gap_min"]))
            if checkin_due(cfg, next_checkin):
                spawn(cfg, "checkin")
                next_checkin = next_checkin_time(cfg)
            if t >= next_cleanup:
                cleanup_frames(cfg)
                next_cleanup = t + dt.timedelta(hours=1)
            # Sleep in short slices so a stop from the menu bar or the window lands within a quarter second.
            until = started + max(0.5, cfg["interval_sec"])
            while time.monotonic() < until and not stop_event.is_set():
                if stop_file.exists():
                    stop_event.set()
                    break
                if restart_file.exists() and restart_event is not None:
                    restart_file.unlink(missing_ok=True)
                    restart_event.set()
                    stop_event.set()
                    break
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        end_of_run(cfg)


def end_of_run(cfg):
    """What a stop leaves behind is cleared again when the next run starts, in
    case the last one never got to (killed, crashed, or Ctrl+C under the
    menu bar icon, which ends AppKit's run loop without returning)."""
    stop_request_file(cfg).unlink(missing_ok=True)
    restart_request_file(cfg).unlink(missing_ok=True)
    (cfg["data_dir"] / "paused_until").unlink(missing_ok=True)  # a fresh Start should record, not wake up paused


def cmd_run(cfg, args):
    """The capture loop. With the menu bar icon, AppKit needs the main thread,
    so the loop runs in a worker and stops the icon when it ends."""
    import signal

    lock = single_instance(cfg, "run")
    if not lock:
        print("Already running.")
        return
    end_of_run(cfg)
    # macOS decides the permissions once per process, so what the recorder sees now holds
    # for this run. doctor reads it from here: a terminal would only see its own.
    write_state(cfg, run_started=now().isoformat(), run_pid=os.getpid(),
                permissions={"screen": macos.screen_recording_granted(),
                             "accessibility": macos.accessibility_granted(), "pid": os.getpid()})
    stop_event = threading.Event()
    restart_event = threading.Event()
    tray = None
    if not getattr(args, "no_tray", False):
        try:
            tray = make_tray(cfg, stop_event, restart_event)
        except Exception as e:  # recording matters more than the icon
            log_error(cfg, "tray", e)
    if not tray:
        # The loop is on the main thread here, so Python's handler runs in time.
        signal.signal(signal.SIGTERM, lambda *a: stop_event.set())
        capture_loop(cfg, stop_event, restart_event)
        if restart_event.is_set():
            lock.close()
            spawn(cfg, "run", *(["--no-tray"] if getattr(args, "no_tray", False) else []))
        return

    icon, setup = tray

    def worker():
        try:
            capture_loop(cfg, stop_event, restart_event)
        finally:
            if restart_event.is_set():
                lock.close()  # the new recorder needs the single-instance lock
                spawn(cfg, "run")
            macos.on_main_thread(icon.stop)
            # If the icon doesn't let go, cleanup is done anyway; don't linger.
            quit_later = threading.Timer(5, lambda: os._exit(0))
            quit_later.daemon = True
            quit_later.start()

    loop = threading.Thread(target=worker, daemon=True)
    loop.start()
    # The window's Stop falls back to SIGTERM: let the loop clean up first.
    macos.on_signal(signal.SIGTERM, lambda *a: stop_event.set())
    macos.menu_bar_only()
    try:
        icon.run(setup=setup)
    except Exception as e:
        log_error(cfg, "tray", e)
        # Without the icon, the main run loop still has to turn so the app in
        # front stays current; keep it going until recording stops.
        macos.run_main_loop_while(loop.is_alive)
    stop_event.set()
    loop.join(timeout=10)


# ---------------------------------------------------------------- claude

def find_cli(name, custom_path=""):
    """A CLI by its configured path, else from PATH (see macos.extend_path)."""
    custom = os.path.expanduser(custom_path or "")
    if custom and os.path.isfile(custom) and os.access(custom, os.X_OK):
        return custom
    return shutil.which(name)


def find_claude(claude_path=""):
    return find_cli("claude", claude_path)


# Summary calls load no settings and no CLAUDE.md: not the person's own ~/.claude/CLAUDE.md
# (it has nothing to do with summarising and would go along with every call), and not one
# in this folder, which claude would find from data/. A plan login still works without them;
# auth set up in settings.json (apiKeyHelper, env) doesn't, which the Claude check shows.
# And no transcript is kept: it would be a second copy of every screenshot, in
# ~/.claude/projects, that Screen Audit's cleanup never reaches.
CLAUDE_ISOLATION = ["--setting-sources", "", "--no-session-persistence"]


def claude(prompt, model, cwd, tools=False, timeout=600, extra_dirs=(), effort=None, claude_path=""):
    """One headless Claude Code call. Returns (text, cost_usd)."""
    exe = find_claude(claude_path)
    if not exe:
        raise RuntimeError("claude CLI not found. Install it, log in with `claude`, "
                           "or set claude_path in config.json to its full path")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--strict-mcp-config", *CLAUDE_ISOLATION,
           "--system-prompt", "You are a careful analyst. Follow the user's output format exactly."]
    cmd += ["--tools", "Read", "--allowedTools", "Read"] if tools else ["--tools", ""]
    if effort:
        cmd += ["--effort", effort]
    for d in extra_dirs:  # screenshots can live outside data_dir (frames_location)
        cmd += ["--add-dir", str(d)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       cwd=str(cwd), timeout=timeout, env=macos.env_for(exe))
    if r.returncode != 0:
        try:  # claude reports its failure as JSON on stdout; the tail of it is only stats
            reason = json.loads(r.stdout).get("result")
        except (json.JSONDecodeError, TypeError, AttributeError):
            reason = None
        raise RuntimeError(f"claude exited {r.returncode}: {reason or (r.stderr or r.stdout)[-500:]}")
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return out.get("result", ""), out.get("total_cost_usd") or 0


CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"


def check_claude(cfg, timeout=90):
    """Whether summaries can work: the claude CLI is there and logged in.
    Asks Haiku for one word (a fraction of a cent). Returns {ok, kind,
    message, at}; kind is "ok", "missing" (not installed or not found),
    "login" (installed, not logged in or no plan) or "error"."""
    at = now().isoformat()
    exe = find_claude(cfg.get("claude_path"))
    if not exe:
        return {"ok": False, "kind": "missing", "message": None, "at": at}
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", "--model", "haiku", "--output-format", "json", "--strict-mcp-config", *CLAUDE_ISOLATION,
           "--tools", "",
           "--system-prompt", "Reply with the single word OK."]
    try:
        r = subprocess.run(cmd, input="OK?", capture_output=True, text=True, encoding="utf-8",
                           cwd=str(cfg["data_dir"]), timeout=timeout, env=macos.env_for(exe))
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "error", "message": f"No answer from claude within {timeout} s.", "at": at}
    except OSError as e:
        return {"ok": False, "kind": "missing", "message": str(e), "at": at}
    try:
        out = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        out = None
    if r.returncode == 0 and isinstance(out, dict) and not out.get("is_error"):
        return {"ok": True, "kind": "ok", "message": None, "at": at}
    msg = str((out or {}).get("result") or r.stderr or r.stdout or f"claude exited {r.returncode}").strip()
    login = re.search(r"log ?in|/login|auth|api key|credit|subscription|unauthori[sz]ed|401|403", msg, re.I)
    return {"ok": False, "kind": "login" if login else "error", "message": msg[-300:], "at": at}


CODEX_INSTALL = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
CODEX_SYSTEM = ("You are a careful analyst writing from screenshots and logs of one person's work. Answer only "
                "from what you are given. Follow the output format in the request exactly, whatever other "
                "instructions about style or format you may have.")

# Codex is an agent: by default it can run shell commands, read any file,
# search the web and call the person's own connected tools. Screen Audit
# shows it other people's messages and web pages, so all of that is off,
# leaving it what Claude gets here: the prompt and the images.
CODEX_FEATURES_OFF = ["shell_tool", "unified_exec", "view_image", "apps", "plugins", "hooks", "skill_search",
                      "tool_suggest", "browser_use", "browser_use_external", "in_app_browser", "computer_use",
                      "image_generation", "goals", "sleep_tool", "code_mode_host", "multi_agent"]
CODEX_TOOLS_OFF = ["exec_command", "write_stdin", "view_image"]  # what the Mac check confirms is gone


def codex_lockdown(instructions_file):
    """Options that apply to `codex exec` and `codex debug prompt-input` alike."""
    args = ["-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0", "-c", "include_environment_context=false",
            "-c", "include_apps_instructions=false", "-c", f"model_instructions_file={json.dumps(str(instructions_file))}"]
    for feature in CODEX_FEATURES_OFF:
        args += ["--disable", feature]
    return args


def find_codex(codex_path=""):
    return find_cli("codex", codex_path)


def codex(prompt, cwd, model="", effort=None, images=(), timeout=600, codex_path=""):
    """One non-interactive Codex call, locked down as above, without the
    person's own Codex settings, rules or AGENTS.md project files.
    Screenshots are attached as images. Returns (text, cost); Codex doesn't
    report a cost, so it is 0."""
    exe = find_codex(codex_path)
    if not exe:
        raise RuntimeError("codex CLI not found. Install it, log in with `codex`, "
                           "or set codex_path in config.json to its full path")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        last = tmp / "answer.txt"
        instructions = tmp / "instructions.md"
        instructions.write_text(CODEX_SYSTEM, encoding="utf-8")
        cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--sandbox", "read-only", "--color", "never", "-C", str(cwd), "-o", str(last), *codex_lockdown(instructions)]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        # Codex splits an --image value at commas, and one missing file fails
        # the whole call, so only existing screenshots go in, under plain names.
        for i, img in enumerate(p for p in map(Path, images) if p.is_file()):
            link = tmp / f"screenshot-{i + 1:02d}{img.suffix}"
            try:
                link.symlink_to(img)
            except OSError:
                shutil.copyfile(img, link)
            cmd += ["-i", str(link)]  # last, so the prompt comes from stdin rather than after -i
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           cwd=str(cwd), timeout=timeout, env=macos.env_for(exe))
        if r.returncode != 0:
            raise RuntimeError(f"codex exited {r.returncode}: {(r.stderr or r.stdout)[-500:]}")
        text = last.read_text(encoding="utf-8") if last.exists() else ""
    if not text.strip():
        raise RuntimeError(f"codex gave no answer: {(r.stderr or r.stdout)[-300:]}")
    return text, 0


LOGIN_PROBLEM = re.compile(r"not logged in|log ?in|/login|sign in|auth|api key|credit|quota|usage limit|session limit|"
                           r"subscription|plan|unauthori[sz]ed|OAuth|401|403|429|token", re.I)


def check_codex(cfg, timeout=90):
    """Like check_claude, for Codex. `codex login status` only reads the saved
    login, so a logged-in result is confirmed with one real one-word request
    on the summaries' model (a revoked login, an API key with no credit or a
    model the plan doesn't include only show up then)."""
    at = now().isoformat()

    def result(kind, message=None):
        return {"ok": kind == "ok", "kind": kind, "message": message, "at": at, "ai": "codex"}

    exe = find_codex(cfg.get("codex_path"))
    if not exe:
        return result("missing")
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30,
                           env=macos.env_for(exe))
    except subprocess.TimeoutExpired:
        return result("error", "No answer from codex login status within 30 s.")
    except OSError as e:
        return result("missing", str(e))
    msg = (r.stdout + r.stderr).strip()
    if re.search(r"not logged in", msg, re.I):
        return result("login", msg[-300:])
    if r.returncode != 0 or not re.search(r"logged in", msg, re.I):
        return result("error", msg[-300:] or f"codex exited {r.returncode}")
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    try:
        codex("Reply with the single word OK.", cfg["data_dir"], model=codex_model(cfg, "analysis"),
              effort="low", timeout=timeout, codex_path=cfg.get("codex_path"))
    except subprocess.TimeoutExpired:
        return result("error", f"No answer from codex within {timeout} s.")
    except (RuntimeError, OSError) as e:
        text = str(e)
        return result("login" if LOGIN_PROBLEM.search(text) else "error", text[-300:])
    return result("ok")


def check_ai(cfg):
    """Whether the chosen AI can write summaries; see check_claude."""
    if cfg.get("ai") == "codex":
        return check_codex(cfg)
    return {**check_claude(cfg), "ai": "claude"}


TASK_MODEL_KEY = {"analysis": "analysis_model", "daily": "daily_model", "report": "report_model"}


def codex_model(cfg, task, override=None):
    return override or (cfg.get("codex_models") or {}).get(task) or ""


def model_label(cfg, task, model=None):
    """What wrote a document, for its header line and the window's messages."""
    if cfg.get("ai") == "codex":
        return f"Codex {codex_model(cfg, task, model)}".strip()
    return model or cfg[TASK_MODEL_KEY[task]]


def ask(cfg, task, prompt, images=(), timeout=600, model=None):
    """Send one job to the chosen AI. task is "analysis", "daily" or
    "report"; images are screenshot paths; model overrides the configured
    one. Returns (text, cost_usd)."""
    cwd = cfg["data_dir"]
    if cfg.get("ai") == "codex":
        effort = cfg["codex_effort"].get(task) if isinstance(cfg.get("codex_effort"), dict) else None
        return codex(prompt, cwd, model=codex_model(cfg, task, model), effort=effort, images=images,
                     timeout=timeout, codex_path=cfg.get("codex_path"))
    model = model or cfg[TASK_MODEL_KEY[task]]
    if task == "analysis":
        return claude(prompt, model, cwd, tools=bool(images), extra_dirs={Path(f).parent.parent for f in images},
                      claude_path=cfg["claude_path"])
    return claude(prompt, model, cwd, timeout=timeout, effort=cfg.get("report_effort") if task == "report" else None,
                  claude_path=cfg["claude_path"])


# How the analysis prompt introduces the screenshots: Claude reads them with
# its Read tool, Codex gets them attached.
FRAMES_INTRO = {
    "claude": ("Read each one with the Read tool:", "Read these too, but count them as background"),
    "codex": ("They are attached as images, in this order:", "They are attached after the main ones; count them as background"),
}


def parse_json(text):
    """The first JSON object in a model reply, fences and chatter tolerated."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {text[:300]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- analyze

ANALYZE_PROMPT = """These are screenshots of one person's computer during a {minutes}-minute block ({start} to {end}), about {active} minutes of it active, taken while they worked. They want to find out which of their recurring work could be automated, so for this block work out what they were actually doing.

Window timeline (what was in front, how long):
{timeline}

Screenshots of the screen they were working on, oldest first. {frames_intro}
{frames}

Their other monitor(s), only when something changed there (files ending -sideN; reference material, a video, chat or monitoring they keep beside the main work). {side_intro} unless the timeline shows they switched to that screen:
{side}

Then reply with ONLY this JSON, no prose around it:
{{
  "summary": "one or two plain sentences: what got done in this block",
  "activities": [
    {{
      "activity": "concrete description, e.g. 'writing a proposal for a new client project'",
      "minutes": 0,
      "category": "client delivery | sales & applications | communication | admin & finance | learning | building own tools | personal | other",
      "tools": ["apps and services used"],
      "process": "short stable name of the recurring process this is part of, reused across blocks (e.g. 'client proposal', 'weekly status update'), or null if it looks one-off",
      "trigger": "what probably started it (a message, a schedule, a client request, own idea) or null",
      "automation_note": "what part of this a script or Claude skill could do, or why it needs a human"
    }}
  ],
  "question": "one short, specific question you would ask the person to understand WHY or HOW they did something here, or null"
}}

Rules: minutes across activities add up to roughly the active minutes, not the whole block. Do not copy passwords, keys, personal messages or anyone's private details into the output; describe them generically ("replied to a client message")."""


def timeline_text(entries, interval):
    runs = []
    for e in entries:
        key = (e.get("proc", ""), e.get("title", "")) if e["state"] in ("frame", "same") else (e["state"], "")
        if runs and runs[-1][0] == key:
            runs[-1][1] += interval
        else:
            runs.append([key, interval])
    lines = []
    for (a, b), secs in runs:
        if secs < 20 and a not in ("idle", "paused", "excluded", "locked"):
            continue  # alt-tab noise
        label = f"{a}: {b}" if b else f"[{a}]"
        lines.append(f"- {round(secs / 60, 1)} min  {label[:160]}")
    return "\n".join(lines) or "- (nothing longer than 20s)"


def pick_frames(entries, n, key="frame"):
    if key == "side":  # one path repeats while that screen is unchanged
        frames = list(dict.fromkeys(p for e in entries for p in e.get("side") or []))
    else:
        frames = [e[key] for e in entries if e.get(key)]
    if len(frames) <= n:
        return frames
    step = len(frames) / n
    return [frames[int(i * step + step / 2)] for i in range(n)]


def cmd_analyze(cfg, args):
    lock = single_instance(cfg, "analyze")
    if not lock:
        return
    data, batch = cfg["data_dir"], dt.timedelta(minutes=cfg["batch_minutes"])
    state = read_state(cfg)
    start = dt.datetime.fromisoformat(state["analyzed_until"]) if state.get("analyzed_until") else None
    t_now = now()
    days = sorted(p.stem for p in (data / "activity").glob("*.jsonl"))
    if not days:
        print("No activity logged yet.")
        return
    if start is None:
        start = dt.datetime.fromisoformat(days[0])
    entries = []
    for d in days:
        if d >= start.strftime("%Y-%m-%d"):
            entries += read_jsonl(data / "activity" / f"{d}.jsonl")
    for e in entries:
        e["_t"] = dt.datetime.fromisoformat(e["t"])
    entries = [e for e in entries if e["_t"] >= start and e["state"] in ("frame", "same", "excluded", "idle")]

    def floor(t):
        return t.replace(minute=t.minute - t.minute % cfg["batch_minutes"], second=0)

    done = 0
    # Walk block by block over the backlog, at most a day's worth of work per call.
    block_start = floor(start)
    while block_start + batch <= t_now and done < 96:
        upcoming = [e for e in entries if e["_t"] >= block_start]
        if not upcoming:
            write_state(cfg, analyzed_until=floor(t_now).isoformat())
            break
        if upcoming[0]["_t"] >= block_start + batch:  # skip the empty stretch (nights, pauses)
            block_start = floor(upcoming[0]["_t"])
            continue
        block_end = block_start + batch
        if block_end > t_now:
            break
        block = [e for e in upcoming if e["_t"] < block_end]
        active = [e for e in block if e["state"] in ("frame", "same")]
        if len(active) * cfg["interval_sec"] >= 60:  # skip blocks with under a minute of work
            frames = pick_frames(active, cfg["frames_per_batch"])
            side = pick_frames(active, cfg["side_frames_per_batch"], key="side")
            frames_intro, side_intro = FRAMES_INTRO["codex" if cfg.get("ai") == "codex" else "claude"]
            prompt = ANALYZE_PROMPT.format(frames_intro=frames_intro, side_intro=side_intro,
                minutes=cfg["batch_minutes"], active=round(len(active) * cfg["interval_sec"] / 60, 1),
                start=f"{block_start:%Y-%m-%d %H:%M}", end=f"{block_end:%H:%M}",
                timeline=timeline_text(block, cfg["interval_sec"]),
                frames="\n".join(f"- {frame_path(cfg, f)}" for f in frames) or "- (none saved, use the timeline)",
                side="\n".join(f"- {frame_path(cfg, f)}" for f in side) or "- (none)") + lang_rule(cfg, "analysis")
            try:
                text, cost = ask(cfg, "analysis", prompt, images=[frame_path(cfg, f) for f in frames + side])
                result = parse_json(text)
            except Exception as e:
                append_jsonl(data / "errors.jsonl", {"t": now().isoformat(), "where": "analyze",
                                                     "block": block_start.isoformat(), "error": repr(e)[:800]})
                print(f"{block_start:%H:%M} failed, will retry next run: {e}")
                break
            append_jsonl(data / "analysis" / f"{block_start:%Y-%m-%d}.jsonl", {
                "start": block_start.isoformat(), "end": block_end.isoformat(),
                "active_min": round(len(active) * cfg["interval_sec"] / 60, 1),
                "frames": frames, "side_frames": side, "cost_usd": cost, **result})
            print(f"{block_start:%H:%M} {result.get('summary', '')}")
        write_state(cfg, analyzed_until=block_end.isoformat())
        block_start = block_end
        done += 1


# ---------------------------------------------------------------- check-in

GENERIC_QUESTIONS_UK = [
    "Над чим ви зараз працюєте і для чого це?",
    "Що запустило цю задачу: повідомлення, розклад, клієнт чи ваша власна ідея?",
    "Якби це робив хтось інший, що йому треба було б знати насамперед?",
    "Як часто трапляється саме ця задача?",
    "Що повільне або дратує в тому, що ви зараз робите?",
    "Що вам потрібно мати відкритим для цього (інструменти, файли, інформація)?",
    "Як виглядатиме «готово» для того, що ви зараз робите?",
]

GENERIC_QUESTIONS = [
    "What are you working on right now, and what is it for?",
    "What started this task: a message, a schedule, a client, or your own idea?",
    "If someone else did this for you, what would they need to know first?",
    "How often does this exact task come up?",
    "What's slow or annoying about what you're doing right now?",
    "What do you need open to do this (tools, files, info)?",
    "What would 'done' look like for what you're doing now?",
]


def pick_question(cfg):
    data, t = cfg["data_dir"], now()
    asked = {c.get("question") for c in read_jsonl(data / "checkins" / f"{t:%Y-%m-%d}.jsonl")}
    recent = read_jsonl(data / "analysis" / f"{t:%Y-%m-%d}.jsonl")[-2:]
    for a in reversed(recent):
        q = a.get("question")
        if q and q not in asked and dt.datetime.fromisoformat(a["end"]) > t - dt.timedelta(minutes=45):
            return q, a.get("summary", "")
    pool = GENERIC_QUESTIONS_UK if cfg["language"] == "uk" else GENERIC_QUESTIONS
    fresh = [q for q in pool if q not in asked] or pool
    return random.choice(fresh), ""


def cmd_checkin(cfg, args):
    """The check-in popup: the HTML window in the house style, or the old tkinter one if that can't load."""
    try:
        import webview  # noqa: F401
        import screen_audit_checkin
    except ImportError:
        return cmd_checkin_tk(cfg, args)
    screen_audit_checkin.main()


def cmd_wrapup(cfg, args):
    """End-of-day questions in the same popup style; it starts `daily` when saved.
    Without the popup, the log is written from the screen data alone."""
    try:
        import webview  # noqa: F401
        import screen_audit_checkin
    except ImportError:
        return cmd_daily(cfg, argparse.Namespace(date=None))
    screen_audit_checkin.main(mode="wrapup")


def cmd_checkin_tk(cfg, args):
    import tkinter as tk

    lock = single_instance(cfg, "checkin")
    if not lock:
        return
    question, context = pick_question(cfg)
    _, title, proc, _ = foreground()
    shown = now()
    record = {"t": shown.isoformat(), "question": question, "window": title[:200], "proc": proc,
              "context": context, "answer": None, "result": "timeout"}

    root = tk.Tk()
    root.title("Screen audit check-in")
    root.attributes("-topmost", True)
    root.configure(padx=14, pady=12)
    font = ("Helvetica Neue", 13)
    tk.Label(root, text=question, font=("Helvetica Neue", 14, "bold"), wraplength=420, justify="left").pack(anchor="w")
    if title:
        tk.Label(root, text=f"In front: {title[:90]}", font=("Helvetica Neue", 11), fg="#666",
                 wraplength=420, justify="left").pack(anchor="w", pady=(4, 0))
    box = tk.Text(root, width=56, height=5, font=font, wrap="word")
    box.pack(pady=8)
    box.focus_set()

    def close(result):
        record["result"] = result
        if result == "answered":
            record["answer"] = box.get("1.0", "end").strip()
        if result == "pause":
            (cfg["data_dir"] / "paused_until").write_text(
                (now() + dt.timedelta(hours=1)).isoformat(), encoding="utf-8")
        root.destroy()

    row = tk.Frame(root)
    row.pack(fill="x")
    tk.Button(row, text="Save  (Cmd+Enter)", command=lambda: close("answered")).pack(side="left")
    tk.Button(row, text="Skip", command=lambda: close("skipped")).pack(side="left", padx=6)
    tk.Button(row, text="Pause capture 1h", command=lambda: close("pause")).pack(side="right")
    root.bind("<Control-Return>", lambda e: close("answered"))
    root.bind("<Command-Return>", lambda e: close("answered"))
    root.bind("<Escape>", lambda e: close("skipped"))

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{root.winfo_screenwidth() - w - 24}+{root.winfo_screenheight() - h - 80}")
    root.after(cfg["checkin_timeout_min"] * 60_000, lambda: close("timeout") if box.get("1.0", "end").strip() == "" else None)
    root.mainloop()

    if record["result"] == "answered" and not record["answer"]:
        record["result"] = "skipped"
    append_jsonl(cfg["data_dir"] / "checkins" / f"{shown:%Y-%m-%d}.jsonl", record)


# ---------------------------------------------------------------- report

REPORT_PROMPT = """Below is {days} days of observed work from one person: 15-minute block summaries made from screenshots, their own answers to check-in questions, and time totals per app. Their goal, in their words: automate the vast majority of their non-customer-facing work, keep the parts that need them.

What is already automated or has a tool (do not recommend building these again, but do say if the data shows they are not working or not being used):
{context}

Write a markdown report with these sections, in this order:

## Where the time went
A table of categories with hours and share, then two or three sentences on what stands out.

## Task inventory
Every recurring process you can see, one table row each: process | how often | hours over the period | tools | trigger | the steps as observed. Merge processes that are the same thing under different names. One-off things go in a short list under the table.

## Automation verdicts
For each process: **Schedule it** (can run unattended, a human just glances at the output), **Skill with review** (Claude does the work on demand, a human checks before it goes out), or **Keep human** (customer-facing judgement, relationships, decisions). One line of reasoning each, citing what you saw.

## Build these first
The top five by hours saved per week against effort to build, with a rough hours-saved number and what the first version should do. Be concrete enough that the next Claude session could start building from it.

## Questions to answer before building
Where the data is too thin to judge, ask. Short, specific questions only.

Do not pad. If the data covers only a little time, say so up front and keep conclusions proportionate. Plain language, no em dashes.

---- TIME PER APP (active minutes) ----
{apps}

---- BLOCK SUMMARIES ----
{blocks}

---- CHECK-IN ANSWERS ----
{checkins}

---- END-OF-DAY WRAP-UPS (what they finished, what blocked them) ----
{wrapups}
"""


def cmd_report(cfg, args):
    data, end = cfg["data_dir"], now()
    start = end - dt.timedelta(days=args.days)
    days = [(start + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.days + 1)]

    apps, active_entries = {}, 0
    for d in days:
        for e in read_jsonl(data / "activity" / f"{d}.jsonl"):
            if e["state"] in ("frame", "same"):
                apps[e.get("proc") or "?"] = apps.get(e.get("proc") or "?", 0) + cfg["interval_sec"] / 60
                active_entries += 1
    blocks = [b for d in days for b in read_jsonl(data / "analysis" / f"{d}.jsonl")]
    checkins = [c for d in days for c in read_jsonl(data / "checkins" / f"{d}.jsonl")
                if c.get("answer") or c.get("blocker")]
    wrapups = [(d, w) for d in days if (w := read_wrapup(cfg, d)) and w.get("result") == "answered"]
    if not blocks:
        print("No analysed blocks in that range yet. Run `analyze` first, or capture for longer.")
        return None

    def block_line(b):
        acts = "; ".join(f"{a.get('activity')} ({a.get('minutes')}m, {a.get('category')}, process={a.get('process')}, "
                         f"tools={','.join(a.get('tools') or [])}, note={a.get('automation_note')})"
                         for a in b.get("activities", []))
        return f"[{b['start'][:16]}] {b.get('summary', '')} | {acts}"

    prompt = REPORT_PROMPT.format(
        days=args.days,
        context=context_file().read_text(encoding="utf-8"),
        apps="\n".join(f"- {p}: {round(m)} min" for p, m in sorted(apps.items(), key=lambda x: -x[1])[:25]),
        blocks="\n".join(block_line(b) for b in blocks),
        checkins="\n".join(f"[{c['t'][:16]}] Q: {c['question']} | window: {c.get('window', '')[:80]} | "
                           f"A: {c.get('answer') or '(none)'}" + (f" | blocker: {c['blocker']}" if c.get("blocker") else "")
                           for c in checkins) or "(none answered)",
        wrapups="\n".join(f"[{d}] finished: {w.get('done') or '-'} | blocked by: {w.get('blockers') or '-'}"
                          for d, w in wrapups) or "(none)") + lang_rule(cfg)
    print(f"Writing report from {len(blocks)} blocks, {len(checkins)} check-ins, "
          f"{round(active_entries * cfg['interval_sec'] / 3600, 1)} active hours, {model_label(cfg, 'report', args.model)}...")
    text, cost = ask(cfg, "report", prompt, timeout=3600, model=args.model)
    out = HERE / "reports" / f"report-{end:%Y-%m-%d}.md"
    out.parent.mkdir(exist_ok=True)
    meta = tr(cfg, "report_meta", blocks=len(blocks), checkins=len(checkins), at=f"{end:%Y-%m-%d %H:%M}",
              hours=round(active_entries * cfg["interval_sec"] / 3600, 1), model=model_label(cfg, "report", args.model))
    header = f"# {tr(cfg, 'report_title', start=f'{start:%Y-%m-%d}', end=f'{end:%Y-%m-%d}')}\n\n_{meta}_\n\n"
    out.write_text(header + text.strip() + "\n", encoding="utf-8")
    print(f"Wrote {out}  (cost ${cost:.2f})")
    return out


# ---------------------------------------------------------------- daily log

DAILY_PROMPT = """Below is one working day ({day}, {weekday}) of one person, observed: 15-minute block summaries made from screenshots, their answers to check-in questions during the day, their end-of-day wrap-up answers, and time per app. Write their daily work log: what got done, what got in the way, what's next. They read it that evening and next morning, and may paste parts into a standup or a client update.

Write markdown with exactly these sections:

## In short
Two or three sentences: the day's main outcome and the one thing that most needs attention.

## Done
Group by project or recurring process (use the block "process" names, merged where they're the same thing). Under each, bullets of concrete outcomes ("shipped the settings page", "sent 3 client proposals"), not activities ("worked on"). Put the rough time next to each group heading, like "Website redesign (1h 40m)". Where the person's own words say what was finished, trust them over the screenshots. Do not invent outcomes the data doesn't show; if something was worked on but not visibly finished, it belongs in Open loops.

## Where the time went
A table: category | time. Then one sentence if anything stands out (fragmentation, a long detour, a lot of context switching).

## Blockers
Every blocker the person named (check-in blocker fields, wrap-up), plus friction visible on screen (the same error retried, long waits, repeated back-and-forth). One bullet each: what it was, roughly what it cost, and a concrete next move. Mark ones you inferred from the screen with "(seen on screen)". If there were none, write "None reported."

## Open loops
Things started and not finished, or waiting on someone. One line each.

## Tomorrow
Start with what they said they'd do first, in their words. Then at most three more, picked from open loops and blockers. {yesterday_note}

Rules: personal time (games, videos, shopping, private browsing) is a single "Personal" row in the time table, labelled just "Personal" with no breakdown, and appears nowhere else. Do not copy passwords, keys, private messages or other people's personal details; describe them generically. Plain language, no em dashes, no padding. If the day has little data, say so in one line and keep it short.

---- ACTIVE TIME ----
{active}

---- TIME PER APP (active minutes) ----
{apps}

---- BLOCK SUMMARIES ----
{blocks}

---- CHECK-INS (question, what was in front, answer, blocker) ----
{checkins}

---- END-OF-DAY WRAP-UP ----
{wrapup}
"""


def daily_path(day):
    return HERE / "reports" / "daily" / f"{day}.md"


def read_wrapup(cfg, day):
    p = cfg["data_dir"] / "wrapups" / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def analyze_first(cfg):
    """Summarise every finished block before writing; wait out a running summarise."""
    for _ in range(600):
        lock = single_instance(cfg, "analyze")
        if lock:
            lock.close()
            break
        time.sleep(1)
    cmd_analyze(cfg, None)


def write_daily(cfg, day):
    data = cfg["data_dir"]
    apps, active = {}, 0
    for e in read_jsonl(data / "activity" / f"{day}.jsonl"):
        if e["state"] in ("frame", "same"):
            apps[e.get("proc") or "?"] = apps.get(e.get("proc") or "?", 0) + cfg["interval_sec"] / 60
            active += cfg["interval_sec"] / 60
    blocks = read_jsonl(data / "analysis" / f"{day}.jsonl")
    checkins = [c for c in read_jsonl(data / "checkins" / f"{day}.jsonl") if c.get("answer") or c.get("blocker")]
    wrapup = read_wrapup(cfg, day)
    if not blocks and not checkins and not (wrapup and wrapup.get("result") == "answered"):
        return None

    def block_line(b):
        acts = "; ".join(f"{a.get('activity')} ({a.get('minutes')}m, {a.get('category')}, process={a.get('process')})"
                         for a in b.get("activities", []))
        return f"[{b['start'][11:16]}-{b['end'][11:16]}] {b.get('summary', '')} | {acts}"

    def checkin_line(c):
        return (f"[{c['t'][11:16]}] Q: {c['question']} | window: {c.get('window', '')[:80]} | "
                f"A: {c.get('answer') or '(none)'} | blocker: {c.get('blocker') or '(none)'}")

    w = wrapup if wrapup and wrapup.get("result") == "answered" else None
    wrapup_text = (f"Finished today: {w.get('done') or '(left empty)'}\n"
                   f"Blocked or slowed by: {w.get('blockers') or '(left empty)'}\n"
                   f"First thing tomorrow: {w.get('tomorrow') or '(left empty)'}") if w else "(not answered)"
    d = dt.date.fromisoformat(day)
    prev = read_wrapup(cfg, f"{d - dt.timedelta(days=1)}")
    yesterday_note = (f'Yesterday they planned to start with: "{prev["tomorrow"]}". If today\'s data shows whether '
                      f"that happened, say so in one line at the end of this section.") \
        if prev and prev.get("tomorrow") else ""
    prompt = DAILY_PROMPT.format(
        day=day, weekday=f"{d:%A}", yesterday_note=yesterday_note,
        active=f"{round(active)} min across {len(blocks)} summarised 15-minute blocks",
        apps="\n".join(f"- {p}: {round(m)} min" for p, m in sorted(apps.items(), key=lambda x: -x[1])[:15]) or "- (none)",
        blocks="\n".join(block_line(b) for b in blocks) or "(none)",
        checkins="\n".join(checkin_line(c) for c in checkins) or "(none answered)",
        wrapup=wrapup_text) + lang_rule(cfg)
    text, cost = ask(cfg, "daily", prompt, timeout=900)
    out = daily_path(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    h, m = divmod(round(active), 60)
    meta = tr(cfg, "daily_meta", h=h, m=m, blocks=len(blocks), checkins=len(checkins), at=f"{now():%H:%M}",
              model=model_label(cfg, "daily"), wrapup=tr(cfg, "daily_wrapup") if w else "")
    header = f"# {tr(cfg, 'daily_title', weekday=tr(cfg, 'weekdays')[d.weekday()], day=day)}\n\n_{meta}_\n\n"
    out.write_text(header + text.strip() + "\n", encoding="utf-8")
    return out


def cmd_daily(cfg, args):
    day = args.date or f"{now():%Y-%m-%d}"
    write_state(cfg, daily={"day": day, "busy": True, "started": now().isoformat(), "message": None})
    try:
        if day == f"{now():%Y-%m-%d}":
            analyze_first(cfg)
        out = write_daily(cfg, day)
    except Exception as e:
        append_jsonl(cfg["data_dir"] / "errors.jsonl", {"t": now().isoformat(), "where": "daily", "error": repr(e)[:800]})
        write_state(cfg, daily={"day": day, "busy": False, "ok": False, "message": tr(cfg, "daily_failed", e=e)[:300]})
        print(f"Failed: {e}")
        return None
    if not out:
        write_state(cfg, daily={"day": day, "busy": False, "ok": False, "message": tr(cfg, "daily_empty")})
        print("Nothing to log for that day.")
        return None
    write_state(cfg, daily={"day": day, "busy": False, "ok": True, "message": None})
    print(f"Wrote {out}")
    if not getattr(args, "no_open", False):
        macos.open_path(out)
    return out


# ---------------------------------------------------------------- pause / status

def cmd_pause(cfg, args):
    p = cfg["data_dir"] / "paused_until"
    p.parent.mkdir(parents=True, exist_ok=True)
    if args.minutes == 0:
        p.write_text("forever", encoding="utf-8")
        print("Paused until `resume`.")
    else:
        until = now() + dt.timedelta(minutes=args.minutes)
        p.write_text(until.isoformat(), encoding="utf-8")
        print(f"Paused until {until:%H:%M}.")


def cmd_resume(cfg, args):
    (cfg["data_dir"] / "paused_until").unlink(missing_ok=True)
    print("Capturing again.")


def is_running(cfg):
    lock = single_instance(cfg, "run")
    if lock:
        lock.close()
    return lock is None


def get_status(cfg):
    """Everything the status command and the GUI show, as a dict."""
    data, t = cfg["data_dir"], now()
    counts = {}
    for e in read_jsonl(data / "activity" / f"{t:%Y-%m-%d}.jsonl"):
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    blocks = read_jsonl(data / "analysis" / f"{t:%Y-%m-%d}.jsonl")
    checkins = read_jsonl(data / "checkins" / f"{t:%Y-%m-%d}.jsonl")
    all_days = sorted(p.stem for p in (data / "activity").glob("*.jsonl")) if (data / "activity").exists() else []
    reports = sorted((HERE / "reports").glob("report-*.md")) if (HERE / "reports").exists() else []
    return {
        "data_dir": data,
        "running": is_running(cfg),
        "paused_until": paused_until(cfg),
        "frames": counts.get("frame", 0),
        "active_min": round((counts.get("frame", 0) + counts.get("same", 0)) * cfg["interval_sec"] / 60),
        "excluded": counts.get("excluded", 0),
        "idle": counts.get("idle", 0),
        "blocks": len(blocks),
        "last_summary": blocks[-1].get("summary") if blocks else None,
        "last_block_end": blocks[-1].get("end") if blocks else None,
        "cost_today": sum(b.get("cost_usd", 0) for b in blocks),
        "analyzed_until": read_state(cfg).get("analyzed_until"),
        "checkins_answered": sum(1 for c in checkins if c.get("answer")),
        "checkins_asked": len(checkins),
        "days_recorded": len(all_days),
        "first_day": all_days[0] if all_days else None,
        "last_report": reports[-1] if reports else None,
        "errors": read_jsonl(data / "errors.jsonl")[-3:],
        "daily_file": daily_path(f"{t:%Y-%m-%d}") if daily_path(f"{t:%Y-%m-%d}").exists() else None,
        "daily_job": read_state(cfg).get("daily") or {},
        "wrapup_answered": bool((read_wrapup(cfg, f"{t:%Y-%m-%d}") or {}).get("result") == "answered"),
    }


def cmd_status(cfg, args):
    s = get_status(cfg)
    pu = s["paused_until"]
    print(f"Data dir:   {s['data_dir']}")
    print(f"Capture:    {'running' if s['running'] else 'NOT running'}"
          + (f", paused until {'resume' if pu == dt.datetime.max else f'{pu:%H:%M}'}" if pu else ""))
    print(f"Today:      {s['frames']} frames saved, {s['active_min']} active min, "
          f"{s['excluded']} excluded ticks, {s['idle']} idle ticks")
    print(f"Analysed:   {s['blocks']} blocks today, up to {s['analyzed_until'] or 'never'}, ${s['cost_today']:.2f} today")
    print(f"Check-ins:  {s['checkins_answered']} answered of {s['checkins_asked']} today")
    for e in s["errors"]:
        print(f"Last error: {e['t']} {e['where']}: {e['error'][:200]}")


def cmd_stop(cfg, args):
    if not is_running(cfg):
        print("Not running.")
        return
    stop_recorder(cfg)
    print("Stopped." if not is_running(cfg) else "Still running; see data/background.log.")


def cmd_restart(cfg, args):
    """Through the running recorder, so it comes back the way it was started
    (from the app, with its menu bar icon). If none runs, start one through
    the app, so macOS applies the app's permissions rather than this terminal's."""
    if not is_running(cfg):
        if macos.APP_BUNDLE.exists():
            subprocess.Popen(macos.launch_agent_args())
        else:
            spawn(cfg, "run")
        print("Wasn't running; started it.")
        return
    old = read_state(cfg).get("run_pid")
    restart_request_file(cfg).touch()
    for _ in range(60):
        time.sleep(0.25)
        state = read_state(cfg)
        if state.get("run_pid") != old and is_running(cfg):
            print(f"Restarted (pid {state.get('run_pid')}).")
            return
    print("No restart within 15 s; see data/background.log.")


# ---------------------------------------------------------------- doctor

def doctor_checks(cfg, try_it=False, ai=True):
    """The whole setup, one check at a time, for a person or an assistant
    walking someone through it (AGENTS.md). Each check is a dict: name,
    status ("ok", "warn", "fail" or "info"), detail and, unless ok, fix."""
    out = []

    def add(name, status, detail, fix=None):
        out.append({"name": name, "status": status, "detail": detail, "fix": fix})

    v = sys.version_info
    add("Python", "ok" if v >= (3, 12) else "warn", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
        None if v >= (3, 12) else "Install Python 3.12 or newer from python.org (README step 1).")

    missing = []
    for mod in ("PIL", "pystray", "webview", "Quartz", "AppKit", "ApplicationServices"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    add("Packages", "fail" if missing else "ok", f"missing: {', '.join(missing)}" if missing else "all installed",
        f"{sys.executable} -m pip install -r requirements.txt (README step 3)." if missing else None)

    temp_like = ("Downloads", "Desktop", ".Trash", "/private/var/folders/", "/tmp/")
    risky = any(part in str(HERE) for part in temp_like)
    add("Folder", "warn" if risky else "ok", str(HERE),
        "Move the folder somewhere permanent such as ~/screen-audit-mac, then run make_icon.py again "
        "(README step 2)." if risky else None)

    bundle = macos.APP_BUNDLE
    launcher = bundle / "Contents" / "MacOS" / macos.APP_NAME
    if not launcher.exists():
        add("App", "fail", f"{bundle} not found", f"{sys.executable} make_icon.py (README step 5).")
    elif str(HERE) not in launcher.read_text(encoding="utf-8", errors="replace"):
        add("App", "fail", f"{bundle} starts a different folder",
            f"{sys.executable} make_icon.py, so it starts this folder (README step 5).")
    else:
        add("App", "ok", str(bundle))

    if ai:
        r = check_ai(cfg)
        name = "Codex" if cfg.get("ai") == "codex" else "Claude"
        login = "codex  (sign in, then Ctrl+C)" if name == "Codex" else "claude  (log in, then /exit)"
        fixes = {"missing": f"Install {name} Code CLI (README step 4), or set {name.lower()}_path in config.json.",
                 "login": f"The person runs this in their own Terminal: {login}",
                 "error": "Read the detail; README Troubleshooting."}
        add(f"{name} (writes the summaries)", "ok" if r["ok"] else "fail",
            "answers" if r["ok"] else f"{r['kind']}: {r.get('message') or ''}".strip(": "), fixes.get(r["kind"]))
    else:
        add("AI", "info", "not checked (--no-ai)")

    running = is_running(cfg)
    state = read_state(cfg)
    pu = paused_until(cfg)
    add("Recorder", "ok" if running else "fail",
        ("running" + (", paused" if pu else "")) if running else "not running",
        None if running else "Open Screen Audit and press Start: open ~/Applications/'Screen Audit.app'")

    perms = state.get("permissions") or {}
    labels = {"screen": "Screen Recording", "accessibility": "Accessibility"}
    for key, label in labels.items():
        how = (f"The person switches on Screen Audit in System Settings, Privacy & Security, {label} "
               f"(the window's permissions panel opens it), then: {sys.executable} screen_audit.py restart")
        if not running or perms.get("pid") != state.get("run_pid"):
            add(label, "info", "unknown until the recorder runs (only the app's own answer counts)")
        elif perms.get(key) is False:
            add(label, "fail" if key == "screen" else "warn", "not allowed", how)
        else:
            add(label, "ok", "allowed" if perms.get(key) else "macOS doesn't say (before 11)")

    day = now().strftime("%Y-%m-%d")
    recent = read_jsonl(cfg["data_dir"] / "activity" / f"{day}.jsonl")[-3:]
    if running and recent:
        last = recent[-1]
        age = (now() - dt.datetime.fromisoformat(last["t"])).total_seconds()
        if age > max(60, cfg["interval_sec"] * 6):
            add("Capturing", "fail", f"last tick {int(age)} s ago", "Restart: screen_audit.py restart")
        elif last["state"] == "no_permission":
            add("Capturing", "fail", "every tick says no Screen Recording permission", "See Screen Recording above.")
        else:
            add("Capturing", "ok", f"last tick {int(age)} s ago: {last['state']}")
    elif running:
        add("Capturing", "info", "no ticks yet today; run doctor again in a minute")

    errors = [e for e in read_jsonl(cfg["data_dir"] / "errors.jsonl")[-20:]
              if e.get("where") in ("analyze", "daily", "wrapup")
              and e["t"] > (now() - dt.timedelta(hours=24)).isoformat()]
    if errors:
        add("Summaries", "warn", f"{len(errors)} failed in the last 24 h, last: {str(errors[-1]['error'])[:200]}",
            "Fix the AI check above, then press Summarise now in the window.")
    else:
        done = state.get("analyzed_until")
        add("Summaries", "ok", f"summarised up to {done}" if done else "none yet (the first comes after 15 minutes)")

    add("Start at login", "ok" if macos.start_at_login_enabled() else "info",
        "on" if macos.start_at_login_enabled() else "off (optional: Settings in the window)")

    sounds_on = cfg.get("sounds", True)
    if not try_it:
        add("Sounds", "info", f"{'on' if sounds_on else 'off'}; doctor --try plays them")
        add("Notifications", "info", "doctor --try shows a test one")
    else:
        played = macos.play_wav(cue_wav(CUES["pause"]))
        time.sleep(0.4)
        played = macos.play_wav(cue_wav(CUES["resume"])) and played
        add("Sounds", "ok" if played else "fail",
            "played the pause tone (falling) then the resume tone (rising). Ask the person whether they heard both"
            + ("" if sounds_on else "; they're switched off for recording (\"sounds\": false in config.json)"),
            None if played else "afplay failed: check the Mac's sound output.")
        shown = macos.notify("Screen Audit: test", "If you can read this, alerts work.")
        add("Notifications", "ok" if shown else "fail",
            "sent a banner titled 'Screen Audit: test'. Ask the person whether it appeared",
            "If it didn't: System Settings, Notifications, Script Editor: Allow notifications "
            "(macOS files these banners under Script Editor).")
    return out


def cmd_doctor(cfg, args):
    checks = doctor_checks(cfg, try_it=args.try_it, ai=not args.no_ai)
    if args.json:
        print(json.dumps(checks, indent=2, default=str))
    else:
        for c in checks:
            print(f"{c['status'].upper():5} {c['name']}: {c['detail']}")
            if c["fix"]:
                print(f"      fix: {c['fix']}")
    sys.exit(1 if any(c["status"] == "fail" for c in checks) else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--no-tray", action="store_true")
    sub.add_parser("analyze")
    sub.add_parser("checkin")
    sub.add_parser("wrapup")
    dp = sub.add_parser("daily")
    dp.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    dp.add_argument("--no-open", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--model", default=None)
    pp = sub.add_parser("pause")
    pp.add_argument("minutes", type=int, nargs="?", default=60)
    sub.add_parser("resume")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("restart")
    doc = sub.add_parser("doctor")
    doc.add_argument("--try", dest="try_it", action="store_true", help="also play the sounds and show a test notification")
    doc.add_argument("--no-ai", action="store_true", help="skip the Claude/Codex check (it costs a fraction of a cent)")
    doc.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    {"run": cmd_run, "analyze": cmd_analyze, "checkin": cmd_checkin, "wrapup": cmd_wrapup,
     "daily": cmd_daily, "report": cmd_report,
     "pause": cmd_pause, "resume": cmd_resume, "status": cmd_status,
     "stop": cmd_stop, "restart": cmd_restart, "doctor": cmd_doctor}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
