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
from secret_fields import HIDDEN, SECRET_AUTOCOMPLETE, is_secret_label
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

# Attributes observe()/element_summary() need from one element, read in the
# page itself. Attributes are the HTML attributes (like Playwright's
# get_attribute), `value` is the live typed value, `inner` is innerText.
_ATTRS_JS_BODY = """
    const attr = n => el.getAttribute(n);
    const tag = el.tagName.toLowerCase();
    const item = {
        tag, role: attr('role'), type: attr('type'), aria: attr('aria-label'), placeholder: attr('placeholder'),
        inner: el.innerText || '', valueAttr: attr('value'), name: attr('name'), id: attr('id'),
        autocomplete: attr('autocomplete'), checked: !!el.checked,
        // The text of an associated <label> (wrapping or for=id), and of any
        // aria-labelledby targets -- how most real checkboxes, radios and
        // fields are named. Without these a wrapped checkbox read as ''.
        labelText: el.labels && el.labels.length ? el.labels[0].innerText || '' : '',
        labelledBy: (attr('aria-labelledby') || '').split(/\\s+/).filter(Boolean)
            .map(id => (document.getElementById(id) || {}).innerText || '').join(' '),
        value: (tag === 'input' || tag === 'select' || tag === 'textarea') ? String(el.value ?? '') : '',
    };
"""
_ELEMENT_ATTRS_JS = "el => {" + _ATTRS_JS_BODY + " return item; }"

# Every visible interactive element, in document order, plus the page title
# and body text -- all of observe()'s reads in one round trip. Visibility
# follows Playwright's own is_visible() (which observe() used per element
# before): visible style, and a non-empty box; `display: contents` elements
# have no box of their own, so they count if any child is visible.
_SNAPSHOT_JS = """
(selector) => {
    const styleVisible = (el, style) =>
        (!el.checkVisibility || el.checkVisibility()) && style.visibility === 'visible';
    const isVisible = (el) => {
        const style = getComputedStyle(el);
        if (style.display === 'contents') {
            return Array.from(el.children).some(isVisible);
        }
        if (!styleVisible(el, style)) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const items = [], kept = [];
    for (const el of document.querySelectorAll(selector)) {
        try {
            if (!isVisible(el)) continue;
""" + _ATTRS_JS_BODY + """
            items.push(item);
            kept.push(el);
        } catch (e) { /* skip an element that can't be read, as before */ }
    }
    return {items, kept, title: document.title, body: document.body ? document.body.innerText : ''};
}
"""


@dataclass
class ElementInfo:
    index: int
    tag: str
    role: str
    text: str
    input_type: str = ""
    state: str = ""  # current .checked (checkbox/radio) or .value (everything else; HIDDEN for a secret field) -- see observe()


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
    # Whether `visible_text` is a partial window into a longer page (see
    # BrowserSession.scroll()/observe()) -- surfaced to the model (see
    # llm.py) so it never mistakes "not mentioned in what I've read so
    # far" for "not on the page at all" on a long article/document/result
    # list, and knows `scroll` will reveal the rest.
    text_truncated: bool
    total_text_length: int


# See BrowserSession.is_search_submit(). Google's box is a <textarea
# name="q"> in <form role="search" action="/search"> (GET); YouTube's is
# name="search_query" in action="/results"; Wikipedia's is type="search".
_SEARCH_SUBMIT_JS = r"""el => {
  const form = el.form || el.closest('form');
  if (!form || !['input', 'textarea'].includes(el.tagName.toLowerCase())) return false;
  if ((form.getAttribute('method') || 'get').toLowerCase() !== 'get') return false;
  if (form.querySelector('input[type=password], input[type=file], [formmethod]:not([formmethod=get i])')) return false;
  const name = (el.getAttribute('name') || '').toLowerCase();
  const action = (form.getAttribute('action') || '').toLowerCase();
  return (el.getAttribute('type') || '').toLowerCase() === 'search'
    || (el.getAttribute('role') || '').toLowerCase() === 'searchbox'
    || !!el.closest('[role=search], search')
    || /search|results/.test(action)
    || ['q', 'query', 'search', 'search_query', 'keywords'].includes(name);
}"""


class BrowserSession:
    """Owns the Playwright/Chrome lifecycle for one agent run."""

    def __init__(self, config):
        self.config = config
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._last_elements: list = []  # Playwright ElementHandles, indexed like the observation
        # Text pagination state -- see scroll()/observe(). A page's full
        # inner_text is captured regardless of scroll position (Playwright
        # doesn't limit it to the viewport), so without this, a page longer
        # than max_chars would have its tail permanently unreachable: every
        # observe() would re-slice the exact same first N characters no
        # matter how much the model scrolled. This offset is what makes
        # `scroll` actually page through the text, not just move the
        # (invisible, in headless mode) viewport.
        self._text_offset = 0
        self._last_max_chars = 6000
        self._observed_url: str | None = None

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        # Chrome's "Translate this page?" bubble sits outside the page (the
        # agent never sees it), but a translated page would change the text
        # under the agent's feet -- and it's clutter on a voice run.
        launch_kwargs = {"headless": self.config.headless, "args": ["--disable-features=Translate"]}
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
        self._text_offset = 0  # a freshly navigated-to page is always read from the top

    def go_back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded")
        self._text_offset = 0

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
        # Also page through the text window used by observe() -- see
        # __init__'s comment on _text_offset. Advances by one "page" of
        # text (whatever max_chars the last observe() used), clamped at 0
        # so scrolling up at the top is a no-op rather than going negative.
        self._text_offset = max(0, self._text_offset + (self._last_max_chars if direction == "down" else -self._last_max_chars))

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

        # One in-page read of every interactive element, the title and the
        # body text, instead of ~7 Playwright round trips per element (a
        # visibility check plus one call per attribute). Measured with
        # evals/bench_observe.py -- see CHANGELOG.md 2026-09-24. The element
        # handles for click/type come back from the same snapshot, so the
        # indices below always match the handles, and the page can't change
        # between reading an element and keeping it.
        snapshot = self.page.evaluate_handle(_SNAPSHOT_JS, INTERACTIVE_SELECTOR)
        try:
            data = snapshot.evaluate("s => ({items: s.items, title: s.title, body: s.body})")
            kept_array = snapshot.get_property("kept")
            by_position = kept_array.get_properties()
            kept_handles = [by_position[str(i)].as_element() for i in range(len(data["items"]))]
            kept_array.dispose()
        finally:
            snapshot.dispose()

        elements: list[ElementInfo] = []
        for idx, item in enumerate(data["items"]):
            tag = item["tag"]
            input_type = item["type"] or ""
            secret = self._is_secret_field(tag, input_type, item)
            label = (
                item["aria"]
                or item["labelledBy"]
                or item["placeholder"]
                or item["inner"]
                or item["labelText"]
                # A secret field's value never stands in for its label --
                # the label is sent to the model (see secret_fields.py).
                or (None if secret else item["valueAttr"])
                or item["name"]
                or ""
            )
            label = " ".join(label.split())[:80]  # collapse whitespace, cap length
            if input_type in ("checkbox", "radio"):
                state = "checked" if item["checked"] else "unchecked"
            elif tag in ("input", "select", "textarea"):
                value = item["value"] or ""
                # Masked, but still "" vs HIDDEN, so VERIFY can see that
                # typing into an empty password field did something.
                state = (HIDDEN if value else "") if secret else value
            else:
                state = ""
            elements.append(ElementInfo(
                index=idx, tag=tag, role=item["role"] or tag, text=label, input_type=input_type, state=state,
            ))

        self._last_elements = kept_handles

        # A fresh page (any navigation -- goto, a link click, a submitted
        # form) always starts being read from the top; the offset only
        # persists across observe() calls on the SAME page, which is what
        # makes repeated `scroll` calls page forward through one long page's
        # text instead of getting stuck wherever the previous page left off.
        if self.page.url != self._observed_url:
            self._text_offset = 0
            self._observed_url = self.page.url

        full_text = " ".join((data["body"] or "").split())
        # Clamp so `scroll("down")` called one time too many lands exactly
        # on the last page of text instead of sliding past the end into an
        # empty string forever (Python slicing past the end of a string
        # silently returns "" rather than raising) -- once here, further
        # "scroll down" calls are harmless no-ops, matching a real scrollbar
        # that simply stops at the bottom of the page.
        max_offset = max(0, len(full_text) - max_chars)
        self._text_offset = min(self._text_offset, max_offset)
        visible_text = full_text[self._text_offset:self._text_offset + max_chars]
        text_truncated = self._text_offset + len(visible_text) < len(full_text)
        self._last_max_chars = max_chars

        title = data["title"] or ""
        page_signal = (self.page.url + " " + title + " " + visible_text[:500]).lower()
        looks_like_login = any(phrase in page_signal for phrase in LOGIN_WALL_PHRASES)

        state_fingerprint = "|".join(f"{el.index}:{el.state}" for el in elements)

        return Observation(
            url=self.page.url,
            title=title,
            elements=elements,
            visible_text=visible_text,
            looks_like_login=looks_like_login,
            state_fingerprint=state_fingerprint,
            text_truncated=text_truncated,
            total_text_length=len(full_text),
        )

    def element_summary(self, index: int) -> str:
        """Short human-readable description, used in confirmation prompts and logs."""
        if 0 <= index < len(self._last_elements):
            handle = self._last_elements[index]
            try:
                item = handle.evaluate(_ELEMENT_ATTRS_JS)
                secret = self._is_secret_field(item["tag"], item["type"] or "", item)
                label = (
                    item["inner"]
                    or item["aria"]
                    or item["labelText"]
                    # value is what names an <input type=submit value="Delete">,
                    # but in a secret field it's the secret itself.
                    or (None if secret else item["valueAttr"])
                    or ""
                )
                return f"<{item['tag']}> '{' '.join(label.split())[:60]}'"
            except Exception:
                pass
        return f"element #{index}"

    @staticmethod
    def _is_secret_field(tag: str, input_type: str, attrs: dict) -> bool:
        """Whether this element holds a secret whose value must never be sent
        anywhere -- see secret_fields.py. `attrs` is one element's entry from
        _SNAPSHOT_JS/_ELEMENT_ATTRS_JS. Buttons can't hold one, so a "Reset
        password" button keeps its label."""
        if tag not in ("input", "textarea") or input_type in ("submit", "button", "reset", "checkbox", "radio"):
            return False
        if input_type == "password":
            return True
        autocomplete = (attrs.get("autocomplete") or "").lower().split()
        if SECRET_AUTOCOMPLETE.intersection(autocomplete):
            return True
        return is_secret_label(attrs.get("aria"), attrs.get("placeholder"), attrs.get("name"), attrs.get("id"))

    def is_search_submit(self, index: int) -> bool:
        """Whether typing into this element and pressing Enter is just a
        search: a search box in a form that submits with GET (so the result
        is a plain URL, the same thing `goto` opens without asking), with no
        password/file field and no button overriding the method to POST."""
        if not (0 <= index < len(self._last_elements)):
            return False
        try:
            return bool(self._last_elements[index].evaluate(_SEARCH_SUBMIT_JS))
        except Exception:
            return False  # can't tell -> not a search, so it keeps asking

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
        "description": "Scroll down/up through the CURRENT page's visible text. Use this when the "
                        "observation says the text is truncated -- it reveals the next (or previous) "
                        "chunk on your following observation, letting you read a long page in full "
                        "rather than only ever seeing its first few thousand characters.",
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
            # sensitive -- except a plain search (is_search_submit(): a GET
            # search form, whose result is just a URL `goto` could open
            # without asking). That's R1, asked only with CONFIRM_R1_ACTIONS.
            # Added after a voice "search Google for APC" stopped to ask.
            index = args.get("index")
            if index is not None and self.session.is_search_submit(int(index)):
                return "R1"
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
