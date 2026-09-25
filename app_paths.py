"""
Where the app's files live, for both ways of running it.

From source (python voice.py), everything sits in the project folder.

As a packaged "AI Agent.exe" (build_exe.bat, PyInstaller), the files
bundled into the exe (ui/) are unpacked to sys._MEIPASS, while the files
the person edits or the app writes (.env, logs/, output/, chrome_profile/)
belong next to the exe, where they can find them. Keeping the two apart
here means no other module needs to know whether it's frozen.
"""
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

# .env, logs/, output/, chrome_profile/ -- the folder the person sees.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# Read-only files shipped with the app (ui/).
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
