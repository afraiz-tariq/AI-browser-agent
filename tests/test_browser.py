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
