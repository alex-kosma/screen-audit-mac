"""Draws the app icon (a round character with a recording-light antenna) as
screen_audit.icns and screen_audit.png, and builds the small "Screen Audit"
app in ~/Applications that opens the window. Run once, with the Python that
has the requirements installed: python3 make_icon.py"""

import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 1024  # draw big, downsample for clean edges


RED, AMBER = (232, 72, 60, 255), (240, 170, 40, 255)
TILE, BODY, INK = (205, 226, 250, 255), (250, 249, 246, 255), (27, 28, 31, 255)
CHEEK = (245, 168, 160, 255)


def draw(dot=RED, sleeping=False):
    """A small round character with dot eyes and a recording-light antenna,
    on a soft sky-blue tile. Paused (tray): amber light, eyes closed."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=TILE)
    # antenna: stalk, then the light
    d.line((512, 330, 512, 190), fill=BODY, width=34)
    d.ellipse((512 - 74, 150 - 74, 512 + 74, 150 + 74), fill=dot)
    # body: a soft squircle, a little wider than tall
    d.rounded_rectangle((150, 290, 874, 930), radius=320, fill=BODY)
    # eyes
    ey = 600
    for x in (362, 662):
        if sleeping:
            d.arc((x - 66, ey - 76, x + 66, ey + 44), start=20, end=160, fill=INK, width=34)
        else:
            d.ellipse((x - 62, ey - 88, x + 62, ey + 88), fill=INK)
            d.ellipse((x - 28, ey - 62, x + 4, ey - 30), fill=(255, 255, 255, 255))  # catchlight
    # cheeks and a small smile
    for x in (250, 774):
        d.ellipse((x - 50, 730 - 28, x + 50, 730 + 28), fill=CHEEK)
    d.arc((512 - 48, 700, 512 + 48, 770), start=20, end=160, fill=INK, width=26)
    return img


LAUNCHER = """#!/bin/bash
# Screen Audit launcher, written by make_icon.py. "record" starts the recorder
# (used at login), anything else opens the window.
cd {here}
if [ "$1" = "record" ]; then
  exec {python} {here}/screen_audit.py run
fi
exec {python} {here}/screen_audit_gui.py
"""


def build_app(dest, icns, python=None):
    """A minimal .app bundle whose executable is a shell script that runs
    screen_audit_gui.py with this Python. Opening it from Finder, Launchpad or
    the Dock gives macOS one app to attach the permissions to."""
    python = python or sys.executable
    if dest.exists():
        shutil.rmtree(dest)
    macos_dir, res = dest / "Contents" / "MacOS", dest / "Contents" / "Resources"
    macos_dir.mkdir(parents=True)
    res.mkdir()
    exe = macos_dir / "Screen Audit"
    exe.write_text(LAUNCHER.format(here=shlex.quote(str(HERE)), python=shlex.quote(str(python))), encoding="utf-8")
    exe.chmod(0o755)
    shutil.copy2(icns, res / "ScreenAudit.icns")
    info = {
        "CFBundleName": "Screen Audit", "CFBundleDisplayName": "Screen Audit",
        "CFBundleIdentifier": "com.screenaudit.app", "CFBundleExecutable": "Screen Audit",
        "CFBundleIconFile": "ScreenAudit", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "NSScreenCaptureUsageDescription": "Screen Audit takes a screenshot every few seconds to summarise your work.",
    }
    with open(dest / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    return dest


def main():
    img = draw()
    icns = HERE / "screen_audit.icns"
    img.save(icns, format="ICNS")
    img.resize((256, 256), Image.LANCZOS).save(HERE / "screen_audit.png")
    print(f"Wrote {icns}")
    if sys.platform != "darwin":
        return
    sys.path.insert(0, str(HERE))
    import macos
    app = build_app(macos.APP_BUNDLE, icns)
    # Finder caches icons; touching the bundle makes it look again.
    subprocess.run(["touch", str(app)])
    print(f"Wrote {app}. Open it from Launchpad, Spotlight or Finder, and drag it to the Dock to keep it there.")
    if macos.start_at_login_enabled():  # point the login item at the app from now on
        import screen_audit as sa
        macos.set_start_at_login(True, log=sa.background_log(sa.load_config()))


if __name__ == "__main__":
    main()
