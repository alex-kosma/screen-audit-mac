# Brief: port Screen Audit to macOS

This repository is a copy of a Windows-only Python app, Screen Audit. Turn it
into a macOS app. It will be used by someone else on their Mac. This repo is
only the Mac version, so remove or replace Windows code freely; there is no
Windows version to keep working here.

## What the app does

It takes a screenshot every few seconds and has Claude (through the `claude`
CLI, on the user's own login) summarise each 15-minute block. It asks short
check-in questions in small pywebview popups, with an optional blocker field,
and shows an end-of-day wrap-up popup at a set time. Then it writes a daily
work log and, on demand, a weekly automation report. The UI is HTML pages in
pywebview with a shared design kit (`tokens.css`, `components.css`,
`icons.js`), so it is mostly portable.

## Replace every Windows-specific piece

In `screen_audit.py`, `screen_audit_gui.py`, `screen_audit_checkin.py` and
`make_icon.py`, grep for: ctypes, windll, msvcrt, winreg, 0x08000000,
CREATE_NO_WINDOW, os.startfile, pythonw, System.Windows.Forms, powershell,
.lnk, APPDATA, taskkill, SetCurrentProcessExplicitAppUserModelID.

- **Idle time:** Quartz `CGEventSourceSecondsSinceLastEventType`.
- **Foreground app and window title:** `NSWorkspace.frontmostApplication`
  plus `CGWindowListCopyWindowInfo`. Titles need the Screen Recording
  permission.
- **Monitor rectangles and the active window's screen:** Quartz
  `CGGetActiveDisplayList` / `CGDisplayBounds`, or NSScreen.
- **Screenshots:** `PIL.ImageGrab` works on macOS. Check how it handles
  multiple displays and Retina scaling.
- **Single-instance locks:** `fcntl.flock`.
- **Dark mode:** `defaults read -g AppleInterfaceStyle`. Drop the DWM
  title-bar code.
- **Language detection:** `defaults read -g AppleLanguages`, falling back to
  `locale` (`detect_language()` in `screen_audit.py`).
- **Opening files and folders:** `open`.
- **Background spawn:** `subprocess.Popen(..., start_new_session=True)` with
  `sys.executable`.
- **Start at login:** a LaunchAgent plist in `~/Library/LaunchAgents`,
  instead of the Startup-folder .lnk.
- **Tray:** pystray on macOS must run on the main thread. Restructure
  `cmd_run` / `start_tray` so the capture loop runs in a worker thread.
- **Popup placement** (`place()` in `screen_audit_checkin.py`): check-ins go
  bottom-right of the main screen's visible frame; the wrap-up goes in the
  centre. Use AppKit for the screen size.
- **`make_icon.py`:** produce an `.icns`. Replace the desktop-shortcut step
  with a small launcher: a minimal `.app` bundle that runs
  `screen_audit_gui.py`, or a documented `py2app` build.
- **First run:** a clear explanation, in the existing window, of the Screen
  Recording and Accessibility permissions. Add a button that opens the right
  System Settings pane, and detect whether each permission is granted.
- **Privacy defaults:** add common macOS apps to the excluded list: Keychain
  Access, 1Password, Bitwarden and the macOS Passwords app.

## Keep as it is

- The Claude prompts, summaries, check-ins, end-of-day wrap-up, daily log and
  report.
- The three HTML pages.
- English and Ukrainian (`i18n.js` and `TEXT` in `screen_audit.py`). Change
  "Start recording when Windows starts" to macOS wording in both languages.

## Also

- **`requirements.txt`:** add pyobjc-framework-Quartz and
  pyobjc-framework-Cocoa.
- **README:** rewrite setup and troubleshooting for macOS. Cover the Python
  install, `pip install -r requirements.txt`, the `claude` CLI login, the
  permissions, and how to start the app.
- **Personal data:** never commit recorded data. `data/`, `reports/` and
  `config.json` stay gitignored.
- **Style:** plain language, no em dashes in user-facing text, Lucide icons,
  no emojis.

## Testing without a Mac

Make sure everything imports and that the platform-free logic runs:

- building the block-analysis prompt
- `write_daily` with `claude()` stubbed
- `pick_question`
- `wrapup_due`

In the PR description, list exactly which parts need a first run on a real
Mac to confirm.

## Done

- A PR against `main` with the Mac version.
- A README a non-developer could follow.
- An honest list of what's untested.
