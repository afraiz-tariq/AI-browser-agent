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
