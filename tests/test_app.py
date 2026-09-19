from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).parents[1] / "app.py"


def button_by_label(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def test_streamlit_flow_reaches_and_submits_human_dialog() -> None:
    app = AppTest.from_file(str(APP_PATH))
    app.run(timeout=20)
    assert not app.exception

    for _ in range(5):
        button_by_label(app, "下一轮").click().run(timeout=20)
        assert not app.exception

    assert [item["action"] for item in app.session_state["trajectory"]] == [
        "Structured",
        "LLM",
        "Structured",
        "Structured",
        "LLM",
    ]

    button_by_label(app, "下一轮").click().run(timeout=20)
    assert not app.exception
    assert app.session_state["pending_human"]["target_slot"] == "dyspnea"

    human_input = next(
        area for area in app.text_area if area.label == "医生输入下一条问题"
    )
    human_input.set_value("胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？")
    button_by_label(app, "提交问题").click().run(timeout=20)
    assert not app.exception

    state = app.session_state["clinical_state"]
    assert state.turn == 6
    assert state.previous_channel == "Human"
    assert state.previous_success is True
    assert state.human_budget == 0
    assert state.current_completeness == 3 / 7
