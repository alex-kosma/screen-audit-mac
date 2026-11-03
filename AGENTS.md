# Setting up Screen Audit with an assistant

You are helping someone install and check Screen Audit on their Mac. They
are probably not technical. This file is for you, the assistant; the
person's own guide is README.md, and its steps are the ones below. (If
you've been asked to change Screen Audit itself rather than set it up,
this file doesn't apply; see MAC_PORT.md and DESIGN_RULES.md.)

## How to work

- Talk plainly, one step at a time. Say what you're about to run and why
  before you run it.
- After each step, run the doctor and go by what it says, not by what you
  expect:

  ```bash
  python3 screen_audit.py doctor
  ```

  Every line is `OK`, `WARN`, `FAIL` or `INFO`, and anything not OK comes
  with a `fix:` line. Add `--json` if you'd rather parse it. It exits 1
  while anything fails. The Claude/Codex check makes one tiny request (a
  fraction of a cent); `--no-ai` skips it.
- Setup isn't finished until `doctor --try` shows no FAIL and the person
  has confirmed the two things only they can notice (step 8).
- This is a setup task. Don't change the code to make a check pass. If
  something looks like a bug, say so and stop.

## What only the person can do

Ask them to do these, tell them exactly what to click, and wait:

- **Logging in to Claude or Codex.** It's interactive and theirs. They run
  `claude` (then `/exit`) or `codex` (then Ctrl+C) in their own Terminal
  window. Never ask for or type their password.
- **Allowing permissions** in System Settings, Privacy & Security: Screen
  Recording (required) and Accessibility (recommended), for **Screen
  Audit**. The window's permissions panel has buttons that open the right
  page.
- **Listening and looking** in step 8.

## Steps

0. **Get the folder**, unless you're already in it. It goes in
   `~/screen-audit-mac`. If that folder already exists, it's an earlier
   install with the person's recordings in `data/`: don't replace it, ask.
   No git needed:

   ```bash
   curl -fsSL https://github.com/alex-kosma/screen-audit-mac/archive/refs/heads/main.zip -o /tmp/screen-audit.zip
   unzip -q /tmp/screen-audit.zip -d /tmp/screen-audit && mv /tmp/screen-audit/screen-audit-mac-main ~/screen-audit-mac
   cd ~/screen-audit-mac
   ```

   Run every later command from this folder.
1. **Python 3.12 or newer** (README step 1). `python3 --version`. If it's
   older or missing and Homebrew is there (`brew --version`), you can run
   `brew install python@3.12` and use `python3.12` from then on. Otherwise
   they install it from python.org (it asks for their Mac password, so
   it's theirs to do). If `python3` makes macOS offer the "command line
   developer tools", that's fine to accept, but it doesn't replace
   python.org.
2. **A permanent folder** (step 2). The doctor's `Folder` check warns about
   Downloads, Desktop and temporary folders. Moving it later means
   repeating step 5.
3. **Packages** (step 3): `python3 -m pip install -r requirements.txt`. On
   "externally-managed-environment", use the `.venv` route from the README
   and use `.venv/bin/python` instead of `python3` from then on, including
   for the doctor.
4. **Claude Code or Codex** (step 4). Ask which plan they have: a Claude
   plan means Claude Code, a ChatGPT plan means Codex. Run the install
   line, then have them log in (see above). With Codex, they choose
   **Codex (ChatGPT)** in the window's Settings, AI, after step 6. The
   doctor's AI check confirms the login really works.
5. **The app** (step 5): `python3 make_icon.py`. Doctor: `App` OK.
6. **Start it from the app**, not from your terminal:
   `open ~/Applications/"Screen Audit.app"`, then the person presses
   **Start**. macOS gives permissions to whichever app starts the
   recorder. Started from your terminal, it would ask about the terminal.
7. **Permissions** (step 6). Have the person allow both, then run
   `python3 screen_audit.py restart` (macOS only applies Screen Recording
   to a process started after it was allowed). The doctor shows what the
   recorder itself was given. While the recorder isn't running, those lines
   say `INFO ... unknown`, which is expected.
8. **Check it all:** `python3 screen_audit.py doctor --try`. This plays the
   pause tone (falling) and the resume tone (rising), then shows a
   notification titled "Screen Audit: test". Ask the person whether they
   heard both tones and saw the banner. If the banner didn't appear, it's
   usually because notifications are off for **Script Editor**, which is
   where macOS files these: System Settings, Notifications, Script Editor,
   Allow notifications.
9. **Optional:** in the window's Settings, *Start recording when you log
   in*.

Wait about 20 minutes after starting before you check summaries. The first
one comes after the first 15-minute block. Then `doctor` should show
`Summaries` OK.

## Useful commands

```bash
python3 screen_audit.py status     # today's numbers, as in the window
python3 screen_audit.py restart    # stop the recorder and start a fresh one
python3 screen_audit.py stop
python3 screen_audit.py pause 15   # or resume
```

`data/errors.jsonl` and `data/background.log` have the details behind a
failure. They're personal, so read them for the error and don't paste
their contents anywhere else. README.md, Troubleshooting, covers the
common problems.

## Privacy

Screenshots stay on this Mac, apart from those sent for the summaries to
the AI the person chose. Never upload, share or commit anything in `data/`,
`reports/`, `config.json` or `context.md`.

Before the first recording, ask the person to check their AI account's
training setting (README, Privacy). It's their choice, so don't skip
telling them.
