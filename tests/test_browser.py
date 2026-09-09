"""
Tests for browser.py against the local fixture site (no real internet
needed). Verifies the pieces the agent loop depends on: navigation,
DOM observation, typing+submit, login-wall detection.
"""
from browser import BrowserSession


def test_navigate_and_observe(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        obs = session.observe()
        assert obs.title == "Mock Search Engine"
        assert any(el.tag == "input" for el in obs.elements)
        assert any(el.tag == "button" for el in obs.elements)
        assert obs.looks_like_login is False
    finally:
        session.stop()


def test_type_and_submit_navigates_to_results(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        obs = session.observe()
        input_index = next(el.index for el in obs.elements if el.tag == "input")
        session.type_text(input_index, "OpenAI", submit=True)
        session.wait(500)
        obs2 = session.observe()
        assert "results" in obs2.url
        assert "OpenAI is an AI research" in obs2.visible_text
    finally:
        session.stop()


def test_login_wall_is_detected(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/login_wall.html")
        obs = session.observe()
        assert obs.looks_like_login is True
    finally:
        session.stop()


def test_demo_page_with_password_field_is_not_flagged(test_config, fixtures_server):
    # Regression test: a bare password-type <input> is not proof of a login
    # wall on its own -- plenty of public pages have one (registration
    # forms, "set a new password" forms, input-type demo/test pages like
    # this fixture) without gating any content behind it.
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/demo_page_with_password_field.html")
        obs = session.observe()
        assert obs.looks_like_login is False
    finally:
        session.stop()


def test_normal_page_with_nav_login_link_is_not_flagged(test_config, fixtures_server):
    # Regression test: a real login FORM (password field) must be flagged,
    # but a plain "Log in" nav link -- present on almost every site
    # (Wikipedia, GitHub, any store, ...) -- must NOT be mistaken for one.
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/normal_page_with_nav_login_link.html")
        obs = session.observe()
        assert obs.looks_like_login is False
    finally:
        session.stop()


def test_checkbox_click_changes_state_fingerprint(test_config, fixtures_server):
    # Regression test: clicking a checkbox must be detectable via
    # state_fingerprint even though it changes neither the URL nor the
    # visible text -- this is exactly what the agent loop's VERIFY step
    # relies on to not mistake a working checkbox click for a failed one.
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/checkbox_page.html")
        before = session.observe()
        checkbox_index = next(el.index for el in before.elements if el.input_type == "checkbox")
        assert before.elements[checkbox_index].state == "unchecked"

        session.click(checkbox_index)
        after = session.observe()

        assert after.elements[checkbox_index].state == "checked"
        assert after.state_fingerprint != before.state_fingerprint
        # And, confirming this is genuinely invisible to the old signal:
        assert after.url == before.url
        assert after.visible_text == before.visible_text
    finally:
        session.stop()


def test_sensitive_action_detection(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/login_wall.html")
        obs = session.observe()
        button_index = next(el.index for el in obs.elements if el.tag == "button")
        assert session.is_sensitive(button_index) is False  # "Log in" isn't in our keyword list
        assert "log in" in session.element_summary(button_index).lower()
    finally:
        session.stop()


def test_is_sensitive_detects_a_genuinely_sensitive_button(test_config, fixtures_server):
    # The positive case: the confirmation gate's whole job is to catch
    # buttons like this one before agent.py ever runs the click.
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/sensitive_button.html")
        obs = session.observe()
        button_index = next(el.index for el in obs.elements if el.tag == "button")
        assert "delete" in session.element_summary(button_index).lower()
        assert session.is_sensitive(button_index) is True
    finally:
        session.stop()


def test_is_sensitive_returns_false_for_an_ordinary_button(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/inert_button.html")
        obs = session.observe()
        button_index = next(el.index for el in obs.elements if el.tag == "button")
        assert session.is_sensitive(button_index) is False
    finally:
        session.stop()


def test_click_with_an_invalid_index_raises_indexerror(test_config, fixtures_server):
    # This is exactly what agent.py's loop catches to report "[FAILED:
    # invalid element index]" and continue instead of crashing the task --
    # see _dispatch_action's `except IndexError` in agent.py.
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        session.observe()
        try:
            session.click(999)
            assert False, "expected an IndexError"
        except IndexError as e:
            assert "999" in str(e)
    finally:
        session.stop()


def test_element_summary_falls_back_to_a_generic_label_for_an_invalid_index(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        session.observe()
        assert session.element_summary(999) == "element #999"
    finally:
        session.stop()


def test_go_back_returns_to_the_previous_page(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        session.goto(f"{fixtures_server}/results.html")
        session.go_back()
        obs = session.observe()
        assert obs.title == "Mock Search Engine"
    finally:
        session.stop()


def test_observe_truncates_visible_text_to_max_chars(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/results.html")
        obs = session.observe(max_chars=10)
        assert len(obs.visible_text) <= 10
        assert obs.text_truncated is True
        assert obs.total_text_length > 10
    finally:
        session.stop()


def test_short_page_is_not_reported_as_truncated(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/index.html")
        obs = session.observe()  # default max_chars is far larger than this fixture's text
        assert obs.text_truncated is False
        assert obs.total_text_length == len(obs.visible_text)
    finally:
        session.stop()


def test_scroll_pages_through_a_long_pages_text(test_config, fixtures_server):
    # Regression-shaped: previously `scroll` had zero effect on
    # observe()'s visible_text (Playwright's inner_text() isn't limited to
    # the viewport), so any page longer than max_chars had its tail
    # permanently unreachable no matter how much the model "scrolled".
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/long_page.html")

        first = session.observe(max_chars=2000)
        assert "SEGMENT-00" in first.visible_text
        assert "SEGMENT-23" not in first.visible_text  # the page's last segment, far beyond the first window
        assert first.text_truncated is True

        session.scroll("down")
        second = session.observe(max_chars=2000)
        assert "SEGMENT-00" not in second.visible_text  # scrolled past the start
        assert second.visible_text != first.visible_text

        # Scroll all the way to the end of a page that started out truncated.
        for _ in range(6):
            session.scroll("down")
        last = session.observe(max_chars=2000)
        assert "SEGMENT-23" in last.visible_text
        assert last.text_truncated is False  # nothing left to page through
    finally:
        session.stop()


def test_scroll_up_moves_back_toward_the_start(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/long_page.html")
        session.observe(max_chars=2000)
        session.scroll("down")
        session.scroll("down")
        session.observe(max_chars=2000)

        session.scroll("up")
        session.scroll("up")
        back_at_start = session.observe(max_chars=2000)

        assert "SEGMENT-00" in back_at_start.visible_text
    finally:
        session.stop()


def test_scrolling_up_past_the_start_does_not_go_negative(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/long_page.html")
        session.observe(max_chars=2000)
        session.scroll("up")  # already at the top -- must clamp at 0, not go negative
        obs = session.observe(max_chars=2000)
        assert "SEGMENT-00" in obs.visible_text
    finally:
        session.stop()


def test_navigating_to_a_new_page_resets_the_text_reading_position(test_config, fixtures_server):
    session = BrowserSession(test_config)
    session.start()
    try:
        session.goto(f"{fixtures_server}/long_page.html")
        session.observe(max_chars=2000)
        for _ in range(6):
            session.scroll("down")
        session.observe(max_chars=2000)  # now reading from deep in the page

        session.goto(f"{fixtures_server}/index.html")
        session.goto(f"{fixtures_server}/long_page.html")  # back to the same URL, but a fresh visit
        fresh = session.observe(max_chars=2000)

        assert "SEGMENT-00" in fresh.visible_text
    finally:
        session.stop()
