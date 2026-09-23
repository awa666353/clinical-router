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
    assert app.session_state["pending_human"]["action"] == "Human"

    human_input = next(
        area for area in app.text_area if area.label == "你作为医生要问什么？"
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
    assert len(state.dialogue_history) == 6
    assert app.session_state["trajectory"][-1]["question_slots"] == ["dyspnea"]
    assert "target_slot" not in app.session_state["trajectory"][-1]

    for _ in range(4):
        button_by_label(app, "下一轮").click().run(timeout=20)
        assert not app.exception

    assert app.session_state["clinical_state"].current_completeness == 1.0
    button_by_label(app, "执行 STOP").click().run(timeout=20)
    assert not app.exception
    assert app.session_state["clinical_state"].is_terminated is True
    assert app.session_state["trajectory"][-1]["action"] == "STOP"
    assert app.session_state["trajectory"][-1]["done"] is True


def test_manual_patient_chat_commits_typed_answer() -> None:
    app = AppTest.from_file(str(APP_PATH))
    app.run(timeout=20)
    assert not app.exception

    mode = next(radio for radio in app.radio if radio.label == "体验方式")
    mode.set_value("手动患者聊天").run(timeout=20)
    assert not app.exception

    pending = app.session_state["pending_chat_turn"]
    assert pending["decision"]["action"] == "Structured"
    assert "放射" in pending["expert_result"]["question"]
    assert all(button.label != "开始问诊" for button in app.button)
    assert all(button.label != "生成下一问题" for button in app.button)

    assert len(app.chat_input) == 1
    app.chat_input[0].set_value("会，主要放射到左肩。 ").run(timeout=20)
    assert not app.exception

    state = app.session_state["clinical_state"]
    assert state.turn == 1
    assert state.previous_channel == "Structured"
    assert state.slots["radiation"]["value"] == "会，主要放射到左肩。"
    assert app.session_state["trajectory"][-1]["response_source"] == "manual_patient"
    assert app.session_state["pending_chat_turn"] is not None

    for _ in range(10):
        if app.session_state["clinical_state"].is_terminated:
            break
        assert len(app.chat_input) == 1
        app.chat_input[0].set_value("有，情况比较明显。").run(timeout=20)
        assert not app.exception

    state = app.session_state["clinical_state"]
    assert state.is_terminated is True
    assert state.current_completeness == 1.0
    assert app.session_state["trajectory"][-1]["action"] == "STOP"


def test_manual_chat_opens_human_dialog_automatically() -> None:
    app = AppTest.from_file(str(APP_PATH))
    app.run(timeout=20)
    mode = next(radio for radio in app.radio if radio.label == "体验方式")
    mode.set_value("手动患者聊天").run(timeout=20)

    app.chat_input[0].set_value("我不清楚。").run(timeout=20)
    assert app.session_state["pending_chat_turn"]["decision"]["action"] == "LLM"
    app.chat_input[0].set_value("还是不清楚。").run(timeout=20)
    assert not app.exception

    assert app.session_state["pending_human"]["action"] == "Human"
    human_input = next(
        area for area in app.text_area if area.label == "你作为医生要问什么？"
    )
    human_input.set_value("胸痛会向左肩或者左臂放射吗？")
    button_by_label(app, "提交问题").click().run(timeout=20)
    assert not app.exception
    assert app.session_state["pending_human"] is None
    assert app.session_state["pending_chat_turn"]["decision"]["action"] == "Human"
    assert len(app.chat_input) == 1
