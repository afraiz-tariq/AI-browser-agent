# PyInstaller recipe for the double-clickable "AI Agent.exe" (Windows).
# Build with build_exe.bat (or: pyinstaller ai_agent.spec --noconfirm);
# the app lands in dist\AI Agent\ -- zip that whole folder to hand it on.
#
# It's a folder ("onedir"), not one self-extracting file: it starts in a
# couple of seconds instead of unpacking ~300 MB to a temp folder on every
# launch, and antivirus tools flag one-file Python exes far more often.
#
# Entry point is voice.py: it opens the app window (app_ui.py) with the
# icon by the clock, the same as start_voice.bat. .env, logs/, output/ and
# chrome_profile/ live next to AI Agent.exe (app_paths.py); on the first
# run the bundled .env.example is copied there for the API key.
from PyInstaller.utils.hooks import collect_all, collect_submodules

# The project's own modules are mostly imported lazily (inside functions),
# which PyInstaller's import scan can't see -- list them all.
project_modules = [
    "agent", "app_paths", "app_ui", "browser", "config", "discord_bot", "errors", "excel_tools", "jev",
    "llm", "logger", "mcp_tools", "quick_commands", "secret_fields", "tool_provider", "user_tools",
    "voice_ui", "windows_tools",
]
# Optional packages the app imports lazily; each is bundled when installed
# (build_exe.bat installs them all).
optional = ["anthropic", "openai", "faster_whisper", "sounddevice", "pyttsx3", "pystray", "webview", "pywinauto",
            "comtypes", "PIL", "openpyxl", "discord", "httpx"]

datas = [("ui", "ui"), (".env.example", ".")]
binaries = []
hiddenimports = list(project_modules) + ["pyttsx3.drivers", "pyttsx3.drivers.sapi5", "tkinter"]
for name in optional:
    try:
        d, b, h = collect_all(name)
    except Exception:  # not installed: that feature just isn't in this build
        continue
    datas += d
    binaries += b
    hiddenimports += h

# The exe's icon: the same round icon the tray shows when it's ready.
icon = None
try:
    import os

    from voice_ui import STATUS_COLORS, icon_image

    os.makedirs("build", exist_ok=True)
    icon_image(STATUS_COLORS["ready"], 256).save("build/ai_agent.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    icon = "build/ai_agent.ico"
except Exception:  # no Pillow: PyInstaller's default icon
    pass

a = Analysis(
    ["voice.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tests", "evals"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI Agent",
    console=False,  # no terminal window; output goes to output\voice.log
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AI Agent")
