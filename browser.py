"""
Thin wrapper around Playwright that gives the agent loop two things:

1. A cheap, text-only "observation" of the current page -- a numbered list
   of the interactive elements (links, buttons, inputs, ...) plus a short
   snippet of visible text. This is what gets sent to the LLM instead of a
   screenshot or the raw HTML, which keeps prompts small (and therefore
   cheap) and lets the LLM reason using the accessibility/DOM information
   real assistive tech would use, rather than guessing from pixels.

2. A small set of actions (goto, click, type, scroll, press_enter,
   read_text) that operate on the element indices from that observation,
   so the LLM never has to write CSS selectors by hand.

Everything here is synchronous (Playwright's sync API) to keep the code in
agent.py easy to read top-to-bottom, which matters more than raw speed for
a Phase 1 prototype.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

# Tags we consider "interactive" -- i.e. worth showing to the LLM as
# something it could click/type into. Kept small on purpose: a full DOM
# dump would blow up token usage for no benefit.
INTERACTIVE_SELECTOR = (
    "a, button, input, textarea, select, [role=button], [role=link], "
    "[role=searchbox], [role=textbox], [contenteditable=true]"
)

# Heuristics used to (a) detect that a page wants the user to log in, and
# (b) flag actions that are risky enough to require human confirmation.
LOGIN_HINTS = ("log in", "log-in", "login", "sign in", "signin", "sign-in", "password")
SENSITIVE_KEYWORDS = (
    "submit", "buy", "purchase", "pay", "checkout", "order now", "confirm",
    "send", "delete", "remove", "cancel subscription", "unsubscribe",
    "sign up", "subscribe", "save changes", "update password", "transfer",
)


@dataclass
class ElementInfo:
    index: int
    tag: str
    role: str
    text: str
    input_type: str = ""


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ElementInfo]
    visible_text: str
    looks_like_login: bool


class BrowserSession:
    """Owns the Playwright/Chrome lifecycle for one agent run."""

    def __init__(self, config):
        self.config = config
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._last_elements: list = []  # Playwright ElementHandles, indexed like the observation

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        launch_kwargs = {"headless": self.config.headless}
        if self.config.chrome_executable_path:
            launch_kwargs["executable_path"] = self.config.chrome_executable_path
        else:
            # "chrome" uses the real, locally-installed Google Chrome instead
            # of the bundled Chromium, per the requirement to drive Chrome.
            launch_kwargs["channel"] = self.config.chrome_channel

        if self.config.use_persistent_profile:
            # A persistent profile means cookies/logins the *user* already
            # performed manually in this profile carry over between runs,
            # without the agent ever handling credentials itself.
            self.context = self._playwright.chromium.launch_persistent_context(
                self.config.chrome_user_data_dir, **launch_kwargs
            )
            self.browser = None
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        else:
            self.browser = self._playwright.chromium.launch(**launch_kwargs)
            self.context = self.browser.new_context()
            self.page = self.context.new_page()

        self.page.set_default_timeout(self.config.step_timeout_ms)

    def stop(self) -> None:
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def goto(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded")

    def click(self, index: int) -> None:
        el = self._resolve(index)
        el.scroll_into_view_if_needed()
        el.click()

    def type_text(self, index: int, text: str, submit: bool = False) -> None:
        el = self._resolve(index)
        el.scroll_into_view_if_needed()
        el.click()
        el.fill(text)
        if submit:
            el.press("Enter")

    def press_enter(self, index: int) -> None:
        el = self._resolve(index)
        el.press("Enter")

    def scroll(self, direction: str = "down") -> None:
        delta = 800 if direction == "down" else -800
        self.page.mouse.wheel(0, delta)

    def go_back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded")

    def wait(self, ms: int = 1000) -> None:
        self.page.wait_for_timeout(ms)

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    def observe(self, max_chars: int = 6000) -> Observation:
        """Build a compact, LLM-friendly snapshot of the current page."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.config.step_timeout_ms)
        except PWTimeout:
            pass  # Best-effort; we still try to read whatever is there.

        handles = self.page.query_selector_all(INTERACTIVE_SELECTOR)
        elements: list[ElementInfo] = []
        kept_handles = []
        for i, handle in enumerate(handles):
            try:
                if not handle.is_visible():
                    continue
                tag = handle.evaluate("el => el.tagName.toLowerCase()")
                role = handle.get_attribute("role") or tag
                input_type = handle.get_attribute("type") or ""
                label = (
                    handle.get_attribute("aria-label")
                    or handle.get_attribute("placeholder")
                    or handle.inner_text()
                    or handle.get_attribute("value")
                    or handle.get_attribute("name")
                    or ""
                )
                label = " ".join(label.split())[:80]  # collapse whitespace, cap length
            except Exception:
                continue

            idx = len(kept_handles)
            kept_handles.append(handle)
            elements.append(ElementInfo(index=idx, tag=tag, role=role, text=label, input_type=input_type))

        self._last_elements = kept_handles

        try:
            body_text = self.page.inner_text("body")
        except Exception:
            body_text = ""
        visible_text = " ".join(body_text.split())[:max_chars]

        page_signal = (self.page.url + " " + self.page.title() + " " + visible_text[:500]).lower()
        has_password_field = any(e.input_type == "password" for e in elements)
        looks_like_login = has_password_field or any(hint in page_signal for hint in LOGIN_HINTS)

        return Observation(
            url=self.page.url,
            title=self.page.title(),
            elements=elements,
            visible_text=visible_text,
            looks_like_login=looks_like_login,
        )

    def element_summary(self, index: int) -> str:
        """Short human-readable description, used in confirmation prompts and logs."""
        if 0 <= index < len(self._last_elements):
            handle = self._last_elements[index]
            try:
                tag = handle.evaluate("el => el.tagName.toLowerCase()")
                label = handle.inner_text() or handle.get_attribute("aria-label") or handle.get_attribute("value") or ""
                return f"<{tag}> '{' '.join(label.split())[:60]}'"
            except Exception:
                pass
        return f"element #{index}"

    def is_sensitive(self, index: int) -> bool:
        summary = self.element_summary(index).lower()
        return any(keyword in summary for keyword in SENSITIVE_KEYWORDS)

    def _resolve(self, index: int):
        if not (0 <= index < len(self._last_elements)):
            raise IndexError(
                f"Element #{index} does not exist in the last observation "
                f"(only {len(self._last_elements)} elements were seen)."
            )
        return self._last_elements[index]
