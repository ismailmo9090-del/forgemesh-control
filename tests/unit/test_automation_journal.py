"""The automation journal must not persist typed form values."""

from forgemesh_browser.api.routes.automation import ExecuteRequest, _journal_params


def test_journal_drops_fill_and_type_text() -> None:
    payload = ExecuteRequest(
        action="fill",
        target="label=Password",
        text="hunter2-not-a-real-password",
        tab_id="t1",
    )
    params = _journal_params(payload)
    assert params["action"] == "fill"
    assert params["target"] == "label=Password"
    assert params["text_chars"] == len("hunter2-not-a-real-password")
    assert "text" not in params
    assert "hunter2" not in repr(params)


def test_journal_drops_select_value_and_navigate_text() -> None:
    select = _journal_params(ExecuteRequest(action="select", target="#pick", value="beta"))
    assert select["value_chars"] == 4
    assert "value" not in select

    navigate = _journal_params(
        ExecuteRequest(action="navigate", text="https://site/?token=abc123")
    )
    assert navigate["text_chars"] == len("https://site/?token=abc123")
    assert "abc123" not in repr(navigate)


def test_journal_keeps_step_count_not_step_bodies() -> None:
    steps = [
        {"action": "fill", "target": "#password", "text": "s3cret"},
        {"action": "click", "target": "role=button[name=\"Sign in\"]"},
    ]
    params = _journal_params(
        ExecuteRequest(action="workflow", steps=steps, workflow_name="login")
    )
    assert params["step_count"] == 2
    assert params["workflow_name"] == "login"
    assert "steps" not in params
    assert "s3cret" not in repr(params)


def test_journal_keeps_non_secret_controls() -> None:
    params = _journal_params(
        ExecuteRequest(action="scroll", direction="down", amount=1.5, key=None)
    )
    assert params["direction"] == "down"
    assert params["amount"] == 1.5
    assert "key" not in params

    press = _journal_params(ExecuteRequest(action="press", key="Enter"))
    assert press["key"] == "Enter"
