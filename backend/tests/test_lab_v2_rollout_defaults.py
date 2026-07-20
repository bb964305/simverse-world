from app.config import Settings


def test_protocol_v2_rollout_is_default_deny():
    defaults = Settings.model_fields

    assert defaults["lab_agent_v2_enabled"].default is False
    assert defaults["lab_terminalizer_v2_enabled"].default is False
    assert defaults["lab_outbox_v2_enabled"].default is False
    assert defaults["lab_runtime_v2_canary_enabled"].default is False
    assert defaults["lab_global_admission_enabled"].default is False
