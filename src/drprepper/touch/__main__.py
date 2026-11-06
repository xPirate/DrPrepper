"""drprepper-touch — run the touchscreen reference.

  drprepper-touch                         # windowed 1024x600 (development)
  DRP_FULLSCREEN=1 drprepper-touch        # kiosk mode on the Pi
  drprepper-touch --screenshots out/      # render each screen to PNG and exit (CI / docs)
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="drprepper-touch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="path to drugs.db (default: $DRP_DRUGS_DB or /var/lib/drprepper/drugs.db)")
    ap.add_argument("--screenshots", metavar="DIR", help="save a PNG of each main screen, then exit")
    ap.add_argument("--fullscreen", action="store_true", default=os.environ.get("DRP_FULLSCREEN") == "1")
    a = ap.parse_args(argv)

    # Kivy reads its config at import time, so set it before importing.
    os.environ.setdefault("KIVY_NO_ARGS", "1")
    os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")
    from kivy.config import Config
    Config.set("graphics", "width", "1024")
    Config.set("graphics", "height", "600")
    Config.set("graphics", "resizable", "0")
    Config.set("kivy", "keyboard_mode", "system")  # hardware keys still work; our own on-screen keyboard handles touch
    Config.set("kivy", "exit_on_escape", "0")
    Config.set("input", "mouse", "mouse,multitouch_on_demand")
    if a.fullscreen:
        Config.set("graphics", "fullscreen", "auto")
        Config.set("graphics", "show_cursor", "0")

    from .app import DrPrepperApp
    try:
        DrPrepperApp(db_path=a.db, screenshot_dir=a.screenshots).run()
    except FileNotFoundError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
