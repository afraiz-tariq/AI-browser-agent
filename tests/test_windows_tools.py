"""
Unit tests for the Windows desktop automation arm (windows_tools.py),
without needing a live GUI window: tool spec shape, static and dynamic
(control-text-based) risk classification, index-out-of-range handling, and
the pywinauto call chain mocked at the object level (not a real window).

Requires the optional `pywinauto` package (see requirements.txt) --
skipped entirely if it isn't installed, since this is an opt-in arm
(ENABLE_WINDOWS_AUTOMATION defaults to false) and the rest of the suite
must not depend on it. Real end-to-end correctness against an actual
window can only come from manual verification (see ARCHITECTURE_DECISIONS.md/
CHANGELOG.md for how this arm was verified against a real Notepad window),
not from these mocked tests.
"""
import sys
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pywinauto")

from errors import TaskCannotBeCompleted  # noqa: E402
from tool_provider import requires_confirmation  # noqa: E402
from windows_tools import WindowsAutomationError, WindowsSession, WindowsToolProvider  # noqa: E402


class _FakeConfig:
    def __init__(self, confirm_sensitive_actions=True, confirm_r1_actions=False):
        self.confirm_sensitive_actions = confirm_sensitive_actions
        self.confirm_r1_actions = confirm_r1_actions


def test_get_tool_specs_returns_seven_actions_with_expected_static_risk_tiers():
    # windows_click_control's static tier is R0 -- its real risk is dynamic
    # (see get_dynamic_risk() tests below), mirroring browser.py's click.
    specs = {s.name: s for s in WindowsToolProvider(WindowsSession()).get_tool_specs()}
    assert set(specs) == {
        "windows_launch_app", "windows_list_windows", "windows_list_controls",
        "windows_click_control", "windows_type_into_control", "windows_read_control_text",
        "windows_close_window",
    }
    expected_tiers = {
        "windows_launch_app": "R2",
        "windows_list_windows": "R0",
        "windows_list_controls": "R0",
        "windows_click_control": "R0",
        "windows_type_into_control": "R1",
        "windows_read_control_text": "R0",
        "windows_close_window": "R2",
    }
    for name, expected in expected_tiers.items():
        assert specs[name].risk_level == expected, f"{name} expected {expected}, got {specs[name].risk_level}"


def test_r3_actions_always_confirm_even_with_confirmation_flags_off():
    # R3 is deliberately not configurable off (tool_provider.py) -- a
    # mutating windows_* action must always ask, regardless of
    # CONFIRM_SENSITIVE_ACTIONS/CONFIRM_R1_ACTIONS.
    config = _FakeConfig(confirm_sensitive_actions=False, confirm_r1_actions=False)
    assert requires_confirmation("R3", config) is True


def test_r0_actions_never_confirm_even_with_confirmation_flags_on():
    config = _FakeConfig(confirm_sensitive_actions=True, confirm_r1_actions=True)
    assert requires_confirmation("R0", config) is False


def test_get_control_text_returns_the_controls_accessible_text():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.window_text.return_value = "Delete"
    session._last_controls["Untitled - Notepad"] = [ctrl]

    assert session.get_control_text("Untitled - Notepad", 0) == "Delete"


def test_get_control_text_returns_empty_string_when_not_resolvable():
    # Deliberately forgiving (unlike _resolve_control, used by the actual
    # click/type/read actions, which raises) -- a risk check that can't
    # determine the text should fall through to the static tier, not blow
    # up the whole dispatch. See get_control_text()'s docstring.
    session = WindowsSession()
    assert session.get_control_text("No Such Window", 0) == ""

    session._last_controls["Untitled - Notepad"] = [MagicMock()]
    assert session.get_control_text("Untitled - Notepad", 5) == ""  # out of range


def test_get_dynamic_risk_escalates_click_on_a_sensitive_control():
    # The positive case: a control whose own text matches the sensitive-
    # keyword list must escalate to R2, the same mechanism browser.py's
    # BrowserToolProvider.get_dynamic_risk() uses via is_sensitive().
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.window_text.return_value = "Delete Account"
    session._last_controls["Settings"] = [ctrl]
    provider = WindowsToolProvider(session)

    risk = provider.get_dynamic_risk("windows_click_control", {"window_title": "Settings", "index": 0})

    assert risk == "R2"


def test_get_dynamic_risk_leaves_an_ordinary_control_at_the_static_tier():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.window_text.return_value = "Seven"
    session._last_controls["Calculator"] = [ctrl]
    provider = WindowsToolProvider(session)

    risk = provider.get_dynamic_risk("windows_click_control", {"window_title": "Calculator", "index": 0})

    assert risk is None  # falls through to windows_click_control's static R0


def test_get_dynamic_risk_detects_a_windows_specific_keyword_not_in_browsers_list():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.window_text.return_value = "Uninstall"
    session._last_controls["Programs and Features"] = [ctrl]
    provider = WindowsToolProvider(session)

    risk = provider.get_dynamic_risk(
        "windows_click_control", {"window_title": "Programs and Features", "index": 0}
    )

    assert risk == "R2"


def test_get_dynamic_risk_returns_none_for_non_click_actions():
    provider = WindowsToolProvider(WindowsSession())
    assert provider.get_dynamic_risk("windows_type_into_control", {"window_title": "x", "index": 0}) is None
    assert provider.get_dynamic_risk("windows_launch_app", {"path": "notepad.exe"}) is None
    assert provider.get_dynamic_risk("windows_close_window", {"window_title": "x"}) is None


def test_click_control_before_list_controls_raises_clear_error():
    session = WindowsSession()
    with pytest.raises(WindowsAutomationError, match="windows_list_controls"):
        session.execute("windows_click_control", {"window_title": "Untitled - Notepad", "index": 0})


def test_click_control_with_out_of_range_index_raises_index_error():
    session = WindowsSession()
    session._last_controls["Untitled - Notepad"] = [object(), object()]
    with pytest.raises(IndexError):
        session.execute("windows_click_control", {"window_title": "Untitled - Notepad", "index": 5})


def test_click_control_prefers_invoke_and_clicks_exactly_the_right_control():
    # invoke() (UIA's InvokePattern) is the primary method, not click_input()
    # (real synthetic mouse input at screen coordinates) -- end-to-end
    # testing through the full agent loop found click_input() silently
    # doing nothing whenever another window had focus between steps.
    session = WindowsSession()
    ctrl0, ctrl1 = MagicMock(), MagicMock()
    session._last_controls["Untitled - Notepad"] = [ctrl0, ctrl1]

    session.execute("windows_click_control", {"window_title": "Untitled - Notepad", "index": 1})

    ctrl1.invoke.assert_called_once()
    ctrl1.click_input.assert_not_called()
    ctrl0.invoke.assert_not_called()


def test_click_control_falls_back_to_click_input_when_invoke_unsupported():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.invoke.side_effect = Exception("control has no Invoke pattern")
    session._last_controls["Untitled - Notepad"] = [ctrl]

    session.execute("windows_click_control", {"window_title": "Untitled - Notepad", "index": 0})

    ctrl.click_input.assert_called_once()


def test_type_into_control_prefers_type_keys_and_escapes_special_characters():
    # type_keys() (real simulated keystrokes) is the primary method -- NOT
    # set_edit_text (UIA's ValuePattern), which real-window testing against
    # a modern WinUI-based app found silently writes corrupted text (no
    # exception, just wrong content). type_keys interprets +^%~(){}[] as
    # keystroke-modifier syntax unless escaped, so literal text containing
    # them must come through escaped.
    session = WindowsSession()
    ctrl = MagicMock()
    session._last_controls["Untitled - Notepad"] = [ctrl]

    session.execute(
        "windows_type_into_control", {"window_title": "Untitled - Notepad", "index": 0, "text": "hello (world)"}
    )

    ctrl.set_focus.assert_called_once()
    ctrl.type_keys.assert_called_once_with("hello {(}world{)}", with_spaces=True)
    ctrl.set_edit_text.assert_not_called()


def test_type_into_control_falls_back_to_set_edit_text_when_type_keys_unsupported():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.type_keys.side_effect = Exception("control is not focusable")
    session._last_controls["Untitled - Notepad"] = [ctrl]

    session.execute("windows_type_into_control", {"window_title": "Untitled - Notepad", "index": 0, "text": "hello"})

    ctrl.set_edit_text.assert_called_once_with("hello")


def test_read_control_text_returns_the_controls_window_text():
    session = WindowsSession()
    ctrl = MagicMock()
    ctrl.window_text.return_value = "some value"
    session._last_controls["Untitled - Notepad"] = [ctrl]

    result = session.execute("windows_read_control_text", {"window_title": "Untitled - Notepad", "index": 0})

    assert "some value" in result


def test_describe_for_confirmation_mentions_window_and_control():
    provider = WindowsToolProvider(WindowsSession())
    assert "notepad.exe" in provider.describe_for_confirmation("windows_launch_app", {"path": "notepad.exe"})
    desc = provider.describe_for_confirmation(
        "windows_click_control", {"window_title": "Untitled - Notepad", "index": 3}
    )
    assert "3" in desc and "Untitled - Notepad" in desc
    desc = provider.describe_for_confirmation("windows_close_window", {"window_title": "Untitled - Notepad"})
    assert "Untitled - Notepad" in desc


def test_ensure_ready_raises_clear_error_when_pywinauto_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "pywinauto", None)
    provider = WindowsToolProvider(WindowsSession())
    with pytest.raises(TaskCannotBeCompleted, match="pywinauto"):
        provider.ensure_ready()


def test_list_windows_filters_empty_titles_and_dedupes(monkeypatch):
    import pywinauto

    w1, w2, w3 = MagicMock(), MagicMock(), MagicMock()
    w1.window_text.return_value = "Untitled - Notepad"
    w2.window_text.return_value = ""  # no title -- filtered out
    w3.window_text.return_value = "Untitled - Notepad"  # duplicate -- deduped

    fake_desktop = MagicMock()
    fake_desktop.windows.return_value = [w1, w2, w3]
    monkeypatch.setattr(pywinauto, "Desktop", MagicMock(return_value=fake_desktop))

    result = WindowsSession().execute("windows_list_windows", {})

    assert result == "Open windows: Untitled - Notepad"


def test_launch_app_builds_the_command_line_and_starts_it(monkeypatch):
    import pywinauto.application

    fake_app = MagicMock()
    monkeypatch.setattr(pywinauto.application, "Application", MagicMock(return_value=fake_app))

    result = WindowsSession().execute("windows_launch_app", {"path": "notepad.exe", "args": "C:\\test.txt"})

    fake_app.start.assert_called_once_with('"notepad.exe" C:\\test.txt')
    assert "notepad.exe" in result


def test_list_controls_stores_indexed_controls_for_later_actions(monkeypatch):
    import pywinauto.application

    ctrl0, ctrl1 = MagicMock(), MagicMock()
    ctrl0.friendly_class_name.return_value = "Button"
    ctrl0.window_text.return_value = "OK"
    ctrl1.friendly_class_name.return_value = "Edit"
    ctrl1.window_text.return_value = ""

    fake_window = MagicMock()
    fake_window.descendants.return_value = [ctrl0, ctrl1]
    fake_app = MagicMock()
    fake_app.connect.return_value = fake_app  # pywinauto's connect() returns self
    fake_app.window.return_value = fake_window
    monkeypatch.setattr(pywinauto.application, "Application", MagicMock(return_value=fake_app))

    session = WindowsSession()
    result = session.execute("windows_list_controls", {"window_title": "My Dialog"})

    assert "[0] Button 'OK'" in result
    assert "[1] Edit" in result
    assert session._last_controls["My Dialog"] == [ctrl0, ctrl1]


def test_connect_window_tries_exact_title_match_first(monkeypatch):
    # A real title (as returned by windows_list_windows) must never fall
    # through to substring matching, which is what can silently hit an
    # unrelated window -- see module docstring.
    import pywinauto.application

    fake_window = MagicMock()
    fake_app = MagicMock()
    fake_app.connect.return_value = fake_app
    fake_app.window.return_value = fake_window
    monkeypatch.setattr(pywinauto.application, "Application", MagicMock(return_value=fake_app))

    result = WindowsSession()._connect_window("Untitled - Notepad")

    assert result is fake_window
    fake_app.connect.assert_called_once_with(title="Untitled - Notepad")
    fake_app.window.assert_called_once_with(title="Untitled - Notepad")


def test_connect_window_falls_back_to_substring_when_no_exact_match(monkeypatch):
    import pywinauto.application

    fake_window = MagicMock()
    fake_app = MagicMock()
    fake_app.connect.side_effect = [Exception("no exact match"), fake_app]
    fake_app.window.return_value = fake_window
    monkeypatch.setattr(pywinauto.application, "Application", MagicMock(return_value=fake_app))

    result = WindowsSession()._connect_window("Notepad")

    assert result is fake_window
    assert fake_app.connect.call_count == 2
    _, second_call_kwargs = fake_app.connect.call_args_list[1]
    assert "title_re" in second_call_kwargs


def test_close_window_clears_its_stored_controls(monkeypatch):
    import pywinauto.application

    fake_window = MagicMock()
    fake_app = MagicMock()
    fake_app.connect.return_value = fake_app
    fake_app.window.return_value = fake_window
    monkeypatch.setattr(pywinauto.application, "Application", MagicMock(return_value=fake_app))

    session = WindowsSession()
    session._last_controls["Untitled - Notepad"] = [MagicMock()]

    result = session.execute("windows_close_window", {"window_title": "Untitled - Notepad"})

    fake_window.close.assert_called_once()
    assert "Untitled - Notepad" not in session._last_controls
    assert "Untitled - Notepad" in result
