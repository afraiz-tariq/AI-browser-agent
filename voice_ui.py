"""
The voice assistant's small floating window and tray icon (Windows), so it
feels like an app instead of a terminal: the window shows what it heard,
which step it's on and the result, has a text box to type a task instead
of saying it, and puts up Yes / No buttons when the agent asks before a
risky action; the icon by the clock shows the state and
has Show window / Pause microphone / Open log / Quit.

The pattern other desktop voice agents use (a floating panel with approval
cards, a tray or menu-bar icon -- see docs/JEV_VOICE_PLAN.md), chosen by the
user on 2026-09-25 as the next step after tap-to-talk.

Nothing here decides anything about safety. The buttons feed the same
confirm_callback run_task() already uses (voice.make_voice_confirm): a click
on Yes counts exactly like a spoken "yes", anything else -- No, silence, a
closed window, no answer within the wait -- declines. The buttons exist only
while a question is open, and while it is, run_task() is blocked waiting for
the answer, so the agent itself can never click its own Yes.

tkinter (in Python's Windows installer) and pystray/Pillow (optional: no
tray icon without them) are imported lazily, so the rest of the project and
tests/ don't need them. Choice and NullUi are plain logic, tested offline.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Callable

STATUS_COLORS = {
    "ready": "#9ca3af",      # grey: waiting for you
    "listening": "#ef4444",  # red: the microphone is on
    "working": "#3b82f6",    # blue: running a task
    "asking": "#f59e0b",     # amber: waiting for your yes / no
    "paused": "#4b5563",     # dark grey: microphone paused
    "done": "#22c55e",       # green: finished
    "failed": "#f87171",     # light red: failed
}


class Choice:
    """One answer from the window -- Yes/No (True/False), typed text, or
    Continue/Stop. The first answer wins; later clicks (or a double click)
    change nothing. Thread-safe: the window sets it, the task's thread waits
    on it."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._value: bool | None = None

    def answer(self, value) -> bool:
        """Record an answer; False if one was already recorded."""
        with self._lock:
            if self._event.is_set():
                return False
            self._value = value
            self._event.set()
            return True

    @property
    def decided(self) -> bool:
        return self._event.is_set()

    @property
    def value(self):
        return self._value

    def wait(self, timeout: float):
        self._event.wait(timeout)
        return self._value


class NullUi:
    """No window: every call does nothing (VOICE_UI=false, and tests)."""

    def status(self, kind: str, text: str = "") -> None:
        pass

    def heard(self, text: str) -> None:
        pass

    def step(self, text: str) -> None:
        pass

    def result(self, text: str, ok: bool) -> None:
        pass

    # The richer calls the app window (app_ui.py) uses; here they fall back
    # to the basic ones, so the small tkinter window keeps working unchanged.
    def task(self, text: str, source: str) -> None:
        self.heard(text)

    def agent_step(self, step: int, thought: str, action: str) -> None:
        self.step(f"Step {step}: {thought}")

    def ask(self, prompt: str) -> Choice | None:
        return None  # no buttons: the answer can only be spoken

    def ask_text(self, question: str) -> Choice | None:
        return None  # no text box for answers: spoken only

    def handoff(self, message: str) -> Choice | None:
        return None  # no Continue button: a login wall ends the task

    def end_question(self) -> None:
        pass

    def paused(self, paused: bool, takeover: bool = False) -> None:
        pass

    def info(self, text: str) -> None:
        pass

    def show(self) -> None:
        pass


class Overlay(NullUi):
    """The floating window. Every public method may be called from any
    thread; the work is queued and done on tkinter's own thread (the one
    running root.mainloop())."""

    WIDTH = 380

    PLACEHOLDER = "Type a task and press Enter..."

    def __init__(self, root, hint: str, on_status: Callable[[str], None] | None = None,
                 on_text: Callable[[str], bool] | None = None):
        import tkinter as tk

        self._tk = tk
        self.root = root
        self._calls: queue.Queue = queue.Queue()
        self._on_status = on_status or (lambda kind: None)
        # A typed task goes here (voice.py's submit_typed); False = not taken
        # (still busy), so the text stays in the box.
        self._on_text = on_text
        self._choice: Choice | None = None
        self._offset: tuple[int, int] | None = None  # distance from the bottom-right corner, once dragged

        bg, fg, muted = "#111827", "#f9fafb", "#9ca3af"
        root.title("AI Agent voice")
        root.overrideredirect(True)          # no title bar, no taskbar button
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        root.configure(bg=bg)

        frame = tk.Frame(root, bg=bg, padx=14, pady=10, highlightthickness=1, highlightbackground="#374151")
        frame.pack(fill="both", expand=True)
        top = tk.Frame(frame, bg=bg)
        top.pack(fill="x")
        self._dot = tk.Canvas(top, width=14, height=14, bg=bg, highlightthickness=0)
        self._dot_id = self._dot.create_oval(2, 2, 12, 12, fill=STATUS_COLORS["ready"], outline="")
        self._dot.pack(side="left", padx=(0, 8))
        self._status = tk.Label(top, text="Starting...", bg=bg, fg=fg, font=("Segoe UI", 10, "bold"), anchor="w")
        self._status.pack(side="left", fill="x", expand=True)
        hide = tk.Label(top, text="–", bg=bg, fg=muted, font=("Segoe UI", 12), cursor="hand2")
        hide.pack(side="right")
        hide.bind("<Button-1>", lambda e: self._hide())

        wrap = self.WIDTH - 40
        self._heard = tk.Label(frame, text="", bg=bg, fg=muted, font=("Segoe UI", 9, "italic"),
                               anchor="w", justify="left", wraplength=wrap)
        self._detail = tk.Label(frame, text=hint, bg=bg, fg=fg, font=("Segoe UI", 9),
                                anchor="w", justify="left", wraplength=wrap)
        self._heard.pack(fill="x", pady=(6, 0))
        self._detail.pack(fill="x", pady=(2, 0))

        self._buttons = tk.Frame(frame, bg=bg)
        self._yes = tk.Button(self._buttons, text="Yes", width=9, bg="#16a34a", fg="white", relief="flat",
                              activebackground="#15803d", command=lambda: self._click(True))
        self._no = tk.Button(self._buttons, text="No", width=9, bg="#4b5563", fg="white", relief="flat",
                             activebackground="#374151", command=lambda: self._click(False))
        self._yes.pack(side="left")
        self._no.pack(side="left", padx=(8, 0))

        # The text box: type a task instead of saying it.
        self._entry = None
        if on_text is not None:
            self._entry = tk.Entry(frame, bg="#1f2937", fg=muted, insertbackground=fg, relief="flat",
                                   font=("Segoe UI", 10), highlightthickness=1, highlightbackground="#374151",
                                   highlightcolor="#3b82f6")
            self._entry.insert(0, self.PLACEHOLDER)
            self._entry.pack(fill="x", pady=(8, 0), ipady=4)
            self._entry.bind("<Button-1>", self._focus_entry)
            self._entry.bind("<FocusIn>", lambda e: self._clear_placeholder())
            self._entry.bind("<FocusOut>", lambda e: self._restore_placeholder())
            self._entry.bind("<Return>", lambda e: self._submit())
            self._entry.bind("<Escape>", lambda e: self.root.focus_set())
            self._fg = fg

        for widget in (frame, top, self._status, self._heard, self._detail):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag)

        self._place()
        root.after(50, self._drain)

    # --- thread-safe API (same methods as NullUi) ---------------------------

    def status(self, kind: str, text: str = "") -> None:
        self._later(self._set_status, kind, text)

    def heard(self, text: str) -> None:
        self._later(self._set_label, self._heard, f"“{text}”" if text else "")

    def step(self, text: str) -> None:
        self._later(self._set_label, self._detail, text)

    def result(self, text: str, ok: bool) -> None:
        self._later(self._set_status, "done" if ok else "failed", "Done" if ok else "That didn't work")
        self._later(self._set_label, self._detail, text)

    def ask(self, prompt: str) -> Choice:
        choice = Choice()
        self._later(self._open_question, prompt, choice)
        return choice

    def end_question(self) -> None:
        self._later(self._close_question)

    def show(self) -> None:
        self._later(self._show)

    def quit(self) -> None:
        self._later(self.root.destroy)

    # --- tkinter thread only ------------------------------------------------

    def _later(self, fn, *args) -> None:
        self._calls.put((fn, args))

    def _drain(self) -> None:
        try:
            while True:
                try:
                    fn, args = self._calls.get_nowait()
                except queue.Empty:
                    break
                fn(*args)
            self.root.after(50, self._drain)
        except self._tk.TclError:
            return  # the window is gone (quitting)

    def _set_status(self, kind: str, text: str) -> None:
        self._dot.itemconfigure(self._dot_id, fill=STATUS_COLORS.get(kind, STATUS_COLORS["ready"]))
        if text:
            self._status.configure(text=text)
        self._on_status(kind)
        self._place()

    def _set_label(self, label, text: str) -> None:
        label.configure(text=text)
        self._place()

    def _open_question(self, prompt: str, choice: Choice) -> None:
        self._choice = choice
        self._set_status("asking", "Allow this?")
        self._detail.configure(text=prompt)
        if self._entry is not None:
            self._buttons.pack(anchor="w", pady=(8, 0), before=self._entry)
        else:
            self._buttons.pack(anchor="w", pady=(8, 0))
        self._show()

    def _close_question(self) -> None:
        if self._choice is not None:
            self._choice.answer(False)  # no-op if already answered; a question never stays open
            self._set_status("working", "Working on it...")  # the task carries on (or reports its result)
        self._choice = None
        self._buttons.pack_forget()
        self._place()

    def _click(self, value: bool) -> None:
        if self._choice is not None and self._choice.answer(value):
            self._detail.configure(text=("Yes -- going ahead." if value else "No -- not doing it."))
            self._buttons.pack_forget()
            self._place()

    def _focus_entry(self, event=None) -> None:
        # A window without a title bar doesn't always get the keyboard on a
        # click; ask for it explicitly so typing lands in the box.
        self.root.focus_force()
        self._entry.focus_set()

    def _clear_placeholder(self) -> None:
        if self._entry.get() == self.PLACEHOLDER:
            self._entry.delete(0, "end")
            self._entry.configure(fg=self._fg)

    def _restore_placeholder(self) -> None:
        if not self._entry.get():
            self._entry.insert(0, self.PLACEHOLDER)
            self._entry.configure(fg="#9ca3af")

    def _submit(self) -> None:
        text = self._entry.get().strip()
        if not text or text == self.PLACEHOLDER:
            return
        if self._choice is not None:
            # A question is open: a typed plain "yes" / "no" answers it like
            # the buttons (anything else is left in the box).
            answer = text.lower().strip(" .!")
            if answer in ("yes", "no"):
                self._entry.delete(0, "end")
                self._click(answer == "yes")
            return
        if self._on_text(text):
            self._entry.delete(0, "end")

    def _hide(self) -> None:
        if self._choice is not None:
            return  # keep a question visible until it's answered
        self.root.withdraw()

    def _show(self) -> None:
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._place()

    def _place(self) -> None:
        """Keep the window's bottom-right corner where it was (default: just
        above the taskbar), so growing text never pushes it off-screen."""
        self.root.update_idletasks()
        w, h = self.WIDTH, self.root.winfo_reqheight()
        right, bottom = self._offset or (24, 72)
        x = self.root.winfo_screenwidth() - w - right
        y = self.root.winfo_screenheight() - h - bottom
        self.root.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def _drag_start(self, event) -> None:
        self._drag_from = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag(self, event) -> None:
        x, y = event.x_root - self._drag_from[0], event.y_root - self._drag_from[1]
        self._offset = (self.root.winfo_screenwidth() - self.WIDTH - x,
                        self.root.winfo_screenheight() - self.root.winfo_reqheight() - y)
        self._place()


def icon_image(color: str, size: int = 64):
    """A filled circle for the tray icon (Pillow)."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((6, 6, size - 6, size - 6), fill=color)
    return image


class Tray:
    """The icon by the clock (pystray). start() returns None when pystray or
    Pillow isn't installed; the window works without it."""

    def __init__(self, on_show, on_pause, on_quit, log_path: str):
        import pystray

        self._pystray = pystray
        self.paused = False
        self._on_pause = on_pause
        menu = pystray.Menu(
            pystray.MenuItem("Show window", lambda icon, item: on_show(), default=True),
            pystray.MenuItem("Pause microphone", lambda icon, item: self._toggle_pause(),
                             checked=lambda item: self.paused),
            pystray.MenuItem("Open log", lambda icon, item: _open_file(log_path)),
            pystray.MenuItem("Quit", lambda icon, item: on_quit()),
        )
        self.icon = pystray.Icon("ai-agent-voice", icon_image(STATUS_COLORS["ready"]), "AI Agent voice", menu)

    @classmethod
    def start(cls, on_show, on_pause, on_quit, log_path: str) -> "Tray | None":
        try:
            tray = cls(on_show, on_pause, on_quit, log_path)
        except ImportError:
            return None
        tray.icon.run_detached()
        return tray

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self._on_pause(self.paused)

    def set_status(self, kind: str) -> None:
        try:
            self.icon.icon = icon_image(STATUS_COLORS.get(kind, STATUS_COLORS["ready"]))
        except Exception:  # noqa: BLE001 -- a tray glitch must never stop the assistant
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass


def _open_file(path: str) -> None:
    if os.path.exists(path) and hasattr(os, "startfile"):
        os.startfile(path)  # Windows: opens in the default text editor
