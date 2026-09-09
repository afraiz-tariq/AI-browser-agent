"""
Thin wrapper around Playwright that gives the agent loop two things:

1. A cheap, text-only "observation" of the current page -- a numbered list
   of the interactive elements (links, buttons, inputs, ...) plus a short
   snippet of visible text. This is what gets sent to the LLM instead of a
   screenshot or the raw HTML, which keeps prompts small (and therefore
   cheap) and lets the LLM reason using the accessibility/DOM information
   real assistive tech would use, rather than guessing from pixels.

2. A small set of actions (goto, click, type, scroll, go_back, wait) that
   operate on the element indices from that observation, so the LLM never
   has to write CSS selectors by hand.

Everything here is synchronous (Playwright's sync API) to keep the code in
agent.py easy to read top-to-bottom, which matters more than raw speed for
a Phase 1 prototype.
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

from errors import TaskCannotBeCompleted, explain
from tool_provider import RiskLevel, ToolProvider, ToolSpec

# Tags we consider "interactive" -- i.e. worth showing to the LLM as
# something it could click/type into. Kept small on purpose: a full DOM
# dump would blow up token usage for no benefit.
INTERACTIVE_SELECTOR = (
    "a, button, input, textarea, select, [role=button], [role=link], "
    "[role=searchbox], [role=textbox], [contenteditable=true]"
)

# Heuristics used to (a) fast-path-detect that a page is actively blocking
# access behind a login/verification wall, and (b) flag actions that are
# risky enough to require human confirmation.
#
# Deliberately NOT included here: bare words like "log in" or "password",
# and the mere presence of a password-type <input>. Almost every website's
# nav bar has a "Log in" link (Wikipedia, GitHub, any e-commerce site, ...),
# and plenty of legitimate, fully public pages contain a password field
# without gating anything (registration forms, "set a new password"
# forms, test/demo pages that showcase input types). Matching on either of
# those turns this into a false positive on huge swaths of the web. This
# list is intentionally just a fast, free pre-check for unambiguous wall
# text; the model itself (see llm.py's SYSTEM_PROMPT) is the real judge of
# an actual login form from context, the same way it correctly recognized
# a genuine YC Combinator sign-in page that didn't happen to match any of
# these phrases.
LOGIN_WALL_PHRASES = (
    "sign in to continue", "log in to continue", "please sign in to",
    "please log in to", "you must be logged in", "you must sign in",
    "verify you are human", "verify you're human", "unusual traffic",
    "confirm you are not a robot", "i'm not a robot", "recaptcha",
    "complete the security check", "enter the characters you see",
    "captcha", "access denied", "are you a robot",
)
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
    state: str = ""  # current .checked (checkbox/radio) or .value (everything else) -- see observe()


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ElementInfo]
    visible_text: str
    looks_like_login: bool
    # Joins every element's `state` together. Exists so the agent loop's
    # VERIFY step can tell that an action worked even when it only changes
    # element state (a checkbox toggling, a dropdown's selection) without
    # changing the URL or any visible text -- which visible_text alone
    # can't see, since e.g. a checked checkbox usually renders no new text.
    state_fingerprint: str


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
                if input_type in ("checkbox", "radio"):
                    state = "checked" if handle.evaluate("el => el.checked") else "unchecked"
                elif tag in ("input", "select", "textarea"):
                    state = str(handle.evaluate("el => el.value") or "")
                else:
                    state = ""
            except Exception:
                continue

            idx = len(kept_handles)
            kept_handles.append(handle)
            elements.append(ElementInfo(index=idx, tag=tag, role=role, text=label, input_type=input_type, state=state))

        self._last_elements = kept_handles

        try:
            body_text = self.page.inner_text("body")
        except Exception:
            body_text = ""
        visible_text = " ".join(body_text.split())[:max_chars]

        page_signal = (self.page.url + " " + self.page.title() + " " + visible_text[:500]).lower()
        looks_like_login = any(phrase in page_signal for phrase in LOGIN_WALL_PHRASES)

        state_fingerprint = "|".join(f"{el.index}:{el.state}" for el in elements)

        return Observation(
            url=self.page.url,
            title=self.page.title(),
            elements=elements,
            visible_text=visible_text,
            looks_like_login=looks_like_login,
            state_fingerprint=state_fingerprint,
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


# Registered into llm.py's flat tool list alongside excel_tools.py's specs
# (via BrowserToolProvider.get_tool_specs() below). risk_level here is the
# STATIC/base tier; click and type both start at R0 (no confirmation) and
# get escalated at call time by get_dynamic_risk() below, because their
# real risk depends on which element is targeted / whether a form is being
# submitted -- something that can't be known from the tool name alone.
BROWSER_ACTION_SPECS: dict[str, dict] = {
    "goto": {
        "description": "Navigate the browser to an absolute URL.",
        "properties": {"url": {"type": "string", "description": "Absolute URL to navigate to."}},
        "required": ["url"],
        "risk_level": "R0",
    },
    "click": {
        "description": "Click an interactive element from the CURRENT observation.",
        "properties": {"index": {"type": "integer", "description": "Element index from the CURRENT observation."}},
        "required": ["index"],
        "risk_level": "R0",
    },
    "type": {
        "description": "Type text into an input/textarea element from the CURRENT observation, "
                        "optionally submitting it.",
        "properties": {
            "index": {"type": "integer", "description": "Element index from the CURRENT observation."},
            "text": {"type": "string", "description": "Text to type into the element."},
            "submit": {"type": "boolean", "description": "Press Enter after typing to submit the form."},
        },
        "required": ["index", "text"],
        "risk_level": "R0",
    },
    "scroll": {
        "description": "Scroll the page up or down to reveal more content.",
        "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
        "required": ["direction"],
        "risk_level": "R0",
    },
    "go_back": {
        "description": "Go back to the previous page in browser history.",
        "properties": {},
        "required": [],
        "risk_level": "R0",
    },
    "wait": {
        "description": "Wait for a page to finish loading or settle before observing it again.",
        "properties": {"ms": {"type": "integer", "description": "Milliseconds to wait (default 1000)."}},
        "required": [],
        "risk_level": "R0",
    },
    "extract": {
        "description": "Use when the CURRENT page's visible text already contains what's needed to answer "
                        "the task. No browser action is taken; the loop just re-observes on the next step.",
        "properties": {},
        "required": [],
        "risk_level": "R0",
    },
    "finish": {
        "description": "Call this ONLY when ready to give the final answer. 'summary' must contain the "
                        "actual information/results found (specific facts, names, figures, or extracted "
                        "text) -- never a status confirmation like 'task complete'.",
        "properties": {
            "summary": {
                "type": "string",
                "description": "The complete, self-contained final answer for the user, written in full "
                                "sentences, containing real content from the page(s) visited.",
            }
        },
        "required": ["summary"],
        "risk_level": "R0",  # intercepted by agent.py's loop before any provider dispatch; never confirmed
    },
    "login_required": {
        "description": "Call this if the page shows a login form, CAPTCHA, or MFA/2FA prompt that must "
                        "not be bypassed.",
        "properties": {"reason": {"type": "string", "description": "Why login/verification appears to be required."}},
        "required": ["reason"],
        "risk_level": "R0",  # also intercepted before provider dispatch; see agent.py
    },
}


class BrowserToolProvider(ToolProvider):
    """Wraps a BrowserSession to satisfy the ToolProvider contract. Owns no
    logic of its own beyond dispatch/risk/verify glue -- all the actual
    browser mechanics stay in BrowserSession above, unchanged."""

    def __init__(self, session: BrowserSession):
        self.session = session

    def get_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name, description=spec["description"], properties=spec["properties"],
                required=spec["required"], risk_level=spec["risk_level"],
            )
            for name, spec in BROWSER_ACTION_SPECS.items()
        ]

    def ensure_ready(self) -> None:
        # Chrome is launched lazily on first use, not unconditionally at
        # task start -- a task that never touches the browser arm (a pure
        # Excel task) should never see a Chrome window pop up at all.
        if self.session.page is not None:
            return
        try:
            self.session.start()
        except Exception as e:
            raise TaskCannotBeCompleted(
                explain(
                    "Chrome could not be launched.",
                    "Google Chrome may not be installed, or Playwright cannot find it.",
                    "Install Chrome, then run 'python -m playwright install chrome' and try again.",
                )
            ) from e

    def get_dynamic_risk(self, name: str, args: dict) -> RiskLevel | None:
        if name == "click":
            index = args.get("index")
            if index is not None and self.session.is_sensitive(int(index)):
                return "R2"
        elif name == "type" and args.get("submit"):
            # Confirmed whenever a form is actually being submitted,
            # regardless of whether the target element's own text looks
            # sensitive -- matches the original behavior this replaces.
            return "R2"
        return None

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        if name == "click":
            desc = self.session.element_summary(int(args["index"]))
            return f"click {desc}. This looks like it may have side effects"
        if name == "type":
            desc = self.session.element_summary(int(args["index"]))
            return f"type into {desc} and submit"
        return super().describe_for_confirmation(name, args)

    def execute(self, name: str, args: dict) -> str | None:
        if name == "goto":
            self.session.goto(args["url"])
        elif name == "click":
            self.session.click(int(args["index"]))
        elif name == "type":
            self.session.type_text(
                int(args["index"]), str(args.get("text", "")), submit=bool(args.get("submit", False))
            )
        elif name == "scroll":
            self.session.scroll(args.get("direction", "down"))
        elif name == "go_back":
            self.session.go_back()
        elif name == "wait":
            self.session.wait(int(args.get("ms", 1000)))
        else:
            raise ValueError(f"Unknown browser action: {name!r}")
        return None

    def wants_verification(self, name: str, args: dict) -> bool:
        if name not in ("goto", "click", "type", "go_back"):
            return False
        if name == "type" and not args.get("submit"):
            # A plain type that isn't submitting anything isn't expected
            # to change the URL or page text, so checking would just be
            # noise -- matches the original VERIFIABLE_ACTIONS behavior.
            return False
        return True

    def verify(self, name: str, args: dict, pre_state: dict, post_state) -> str | None:
        """
        The explicit VERIFY step of the observe -> decide -> act -> verify
        loop. `pre_state` is a small dict captured right after the action
        ran (the page state just *before* it); `post_state` is the fresh
        Observation from the very next OBSERVE. If none of the URL, the
        visible text, or any element's state (checked/value --
        state_fingerprint) changed after an action expected to change one
        of them, the action probably didn't do what the model thought.

        Getting this wrong in the "nothing changed" direction is worse
        than it sounds: a real run showed a false "no observable change"
        on a checkbox click (which doesn't add visible text) sent the
        model into a doubt spiral -- re-clicking it repeatedly, second-
        guessing which of two checkboxes was which, until it burned
        through the stuck-loop guard. Comparing state_fingerprint
        alongside the URL/text is what closes that gap for checkboxes,
        radios, dropdowns, and typed values.
        """
        same_url = post_state.url == pre_state["url"]
        same_text = post_state.visible_text == pre_state["text"]
        same_state = post_state.state_fingerprint == pre_state["state"]
        if same_url and same_text and same_state:
            return f"no observable change after {name} {args} -- it may not have worked"
        return None
