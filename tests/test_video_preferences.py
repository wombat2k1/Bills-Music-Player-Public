"""Video Playback preferences: load/save with backward-compatible defaults,
and disabled playback shows a message instead of starting the backend.

Follows the inspect.getsource static-check pattern used elsewhere in this
suite for _load_user_settings/_save_user_settings (both touch too many live
Qt widgets to invoke directly against a fake window).
"""
import inspect
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.media_type import MediaType
from billsmusic.window import PlayerWindow


def test_load_user_settings_persists_video_preferences_with_defaults():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    assert 'cfg.get("video_playback_enabled", True)' in source
    assert 'cfg.get("video_start_fullscreen", False)' in source
    assert 'cfg.get("video_return_to_normal_display_on_end", True)' in source
    assert "VideoTransitionPreferences.from_config(cfg)" in source


def test_save_user_settings_persists_video_preferences():
    source = inspect.getsource(PlayerWindow._save_user_settings)
    assert 'cfg["video_playback_enabled"]' in source
    assert 'cfg["video_start_fullscreen"]' in source
    assert 'cfg["video_return_to_normal_display_on_end"]' in source
    assert 'cfg["video_transitions_enabled"]' in source
    assert 'cfg["video_transition_style"]' in source
    assert 'cfg["video_transition_duration_seconds"]' in source
    assert 'cfg["video_transition_automatic_lead_seconds"]' in source
    assert 'cfg["video_transition_manual_duration_seconds"]' in source
    assert 'cfg["video_transition_enabled_effects"]' in source


def test_dual_video_transitions_compile_time_gate_is_true_but_runtime_gate_defaults_unknown():
    # Two independent gates -- see window.py's DUAL_VIDEO_TRANSITIONS_AVAILABLE
    # docstring comment and CODEX_HANDOFF.md's "Phase 2A" sections for why.
    # The GPU compositor passed extensive standalone validation and is now
    # compiled in (the CPU compositor never was and never will be -- its
    # code stays in video_subprocess.py, permanently unselected). But the
    # compile-time flag alone must never be sufficient to enable the
    # feature -- every PlayerWindow starts with the runtime capability
    # still unknown (None), so the checkbox stays unavailable until an
    # actual GPU probe succeeds (see _start_gpu_capability_probe).
    import billsmusic.window as window_module
    assert window_module.DUAL_VIDEO_TRANSITIONS_AVAILABLE is True
    window = SimpleNamespace(_gpu_dual_capability=None)
    assert not (
        window_module.DUAL_VIDEO_TRANSITIONS_AVAILABLE
        and window._gpu_dual_capability is True
    )


def test_unavailable_reason_reflects_probe_state():
    from billsmusic.window import _dual_transition_unavailable_reason
    checking = _dual_transition_unavailable_reason(None)
    failed = _dual_transition_unavailable_reason(False)
    assert "checking" in checking.lower()
    assert "capability check did not succeed" in failed
    assert checking != failed


def test_load_user_settings_defers_to_apply_gpu_dual_mode_state():
    source = inspect.getsource(PlayerWindow._load_user_settings)
    assert "DualTransitionPreferences.from_config(cfg)" in source
    assert "if not DUAL_VIDEO_TRANSITIONS_AVAILABLE" in source
    assert "DualTransitionPreferences(enabled=False)" in source
    # The actual video_backend.set_dual_mode() call happens exactly once,
    # inside the shared helper -- not duplicated here.
    assert "self._apply_gpu_dual_mode_state()" in source
    assert "set_dual_mode" not in source


def test_apply_gpu_dual_mode_state_requires_both_gates_and_the_preference():
    source = inspect.getsource(PlayerWindow._apply_gpu_dual_mode_state)
    assert "DUAL_VIDEO_TRANSITIONS_AVAILABLE" in source
    assert "self._gpu_dual_capability is True" in source
    assert "video_dual_transitions_enabled" in source
    assert '"gpu" if wants_gpu else None' in source


def test_capability_probe_is_idempotent_and_caches_for_the_session():
    source = inspect.getsource(PlayerWindow._start_gpu_capability_probe)
    assert "self._gpu_dual_capability is not None or self._gpu_dual_probe is not None" in source
    assert "return" in source


def test_preferences_dialog_disables_dual_transition_checkbox_when_unavailable():
    source = inspect.getsource(PlayerWindow._show_normalisation_preferences)
    assert "gpu_dual_available = (" in source
    assert "DUAL_VIDEO_TRANSITIONS_AVAILABLE and self._gpu_dual_capability is True" in source
    assert "video_dual_transitions_enabled.setEnabled(gpu_dual_available)" in source
    assert "_dual_transition_unavailable_reason(self._gpu_dual_capability)" in source
    # The disabled branch also forces the checkbox unchecked, not just
    # non-interactive -- a stale True from an old config.json must not
    # render as a checked-but-greyed-out box.
    assert "video_dual_transitions_enabled.setChecked(False)" in source


def test_preferences_dialog_probes_capability_before_building_the_checkbox():
    source = inspect.getsource(PlayerWindow._show_normalisation_preferences)
    assert "self._start_gpu_capability_probe()" in source


def test_preferences_dialog_save_forces_dual_transitions_off_on_accept():
    source = inspect.getsource(PlayerWindow._show_normalisation_preferences)
    normalized = " ".join(source.split())
    assert (
        "self.video_dual_transitions_enabled = ( "
        "DUAL_VIDEO_TRANSITIONS_AVAILABLE "
        "and self._gpu_dual_capability is True "
        "and video_dual_transitions_enabled.isChecked() )"
    ) in normalized
    assert "self._apply_gpu_dual_mode_state()" in normalized


def test_disabled_video_playback_shows_message_without_starting_backend(tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")
    video_backend = MagicMock()
    messages = []
    window = SimpleNamespace(
        video_playback_enabled=False,
        _video_backend=video_backend,
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda text, *a, **kw: messages.append(text)
        ),
    )

    result = PlayerWindow._play_video_path_direct(window, str(video_path))

    assert result is False
    video_backend.load.assert_not_called()
    assert messages


# -- Real behavioural coverage for the GPU capability gate ------------------

def _fake_window_for_gpu_gate(**overrides):
    configure_dual_calls = []
    defaults = dict(
        _video_backend=MagicMock(),
        _video_transition_manager=SimpleNamespace(
            configure_dual=configure_dual_calls.append
        ),
        _gpu_dual_capability=None,
        _gpu_dual_probe=None,
        video_dual_transitions_enabled=False,
        diagnostics=SimpleNamespace(record=lambda *a, **kw: None),
    )
    defaults.update(overrides)
    window = SimpleNamespace(**defaults)
    window._configure_dual_calls = configure_dual_calls
    return window


def test_apply_gpu_dual_mode_state_enables_gpu_when_all_conditions_met():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with("gpu")
    assert window._configure_dual_calls[-1].enabled is True


def test_apply_gpu_dual_mode_state_falls_back_when_capability_unknown():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=None, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with(None)


def test_apply_gpu_dual_mode_state_falls_back_when_capability_failed():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=False, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with(None)


def test_apply_gpu_dual_mode_state_falls_back_when_preference_off():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=False,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with(None)


def test_apply_gpu_dual_mode_state_no_op_before_video_backend_exists():
    window = _fake_window_for_gpu_gate(_video_backend=None)
    # Must not raise -- this can be reached very early in __init__, before
    # self._video_backend is constructed.
    PlayerWindow._apply_gpu_dual_mode_state(window)


# -- Phase 2B: GPU transition effect preference -----------------------------

def test_apply_gpu_dual_mode_state_passes_configured_gpu_effect_through():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
        video_gpu_transition_effect="Push Left",
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    assert window._configure_dual_calls[-1].gpu_effect == "Push Left"


def test_apply_gpu_dual_mode_state_defaults_gpu_effect_when_attribute_missing():
    # A window object created before this preference existed (or a fake
    # missing it, as every sibling test in this section already is) must
    # not raise -- getattr(..., "Cross Dissolve") is the safe default.
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    assert window._configure_dual_calls[-1].gpu_effect == "Cross Dissolve"


def test_apply_gpu_dual_mode_state_passes_crossfade_fields_through():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
        video_crossfade_audio_enabled=True,
        video_crossfade_audio_curve="Linear",
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    configured = window._configure_dual_calls[-1]
    assert configured.crossfade_video_audio_enabled is True
    assert configured.audio_crossfade_curve == "Linear"


def test_apply_gpu_dual_mode_state_crossfade_forced_off_when_gpu_dual_unavailable():
    # Matches the existing avoid_black_outros/skip_black_intros gating --
    # crossfade has no meaning without the GPU dual engine actually
    # running, so it's force-disabled the same way even if the attribute
    # itself is True.
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=False, video_dual_transitions_enabled=True,
        video_crossfade_audio_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    assert window._configure_dual_calls[-1].crossfade_video_audio_enabled is False


def test_apply_gpu_dual_mode_state_defaults_crossfade_fields_when_missing():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    configured = window._configure_dual_calls[-1]
    assert configured.crossfade_video_audio_enabled is False
    assert configured.audio_crossfade_curve == "Equal Power"


def test_apply_gpu_dual_mode_state_passes_preload_and_timeout_fields_through():
    """Stage B fixed a real wiring gap: _apply_gpu_dual_mode_state's manual
    DualTransitionPreferences reconstruction previously omitted
    preload_lead_seconds/ready_timeout_ms entirely (they silently fell back
    to the dataclass defaults no matter what config.json said), and the two
    new adaptive-timeout fields need the same treatment from the start.
    This proves all four actually reach the live engine's configure_dual
    call, not just DualTransitionPreferences.from_config() in isolation."""
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
        video_dual_preload_lead_seconds=9.5,
        video_dual_ready_timeout_ms=5500,
        video_dual_preload_max_wait_ms=20000,
        video_dual_preload_progress_extension_ms=3000,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    configured = window._configure_dual_calls[-1]
    assert configured.preload_lead_seconds == 9.5
    assert configured.ready_timeout_ms == 5500
    assert configured.preload_max_wait_ms == 20000
    assert configured.preload_progress_extension_ms == 3000


def test_apply_gpu_dual_mode_state_defaults_preload_and_timeout_fields_when_missing():
    # Matches the existing gpu_effect-defaulting test above -- a window
    # object created before these attributes existed must not raise.
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    configured = window._configure_dual_calls[-1]
    assert configured.preload_lead_seconds == 10.0
    assert configured.ready_timeout_ms == 4000
    assert configured.preload_max_wait_ms == 12000
    assert configured.preload_progress_extension_ms == 2000


def test_apply_gpu_dual_mode_state_old_config_gpu_effect_falls_back_when_gpu_unavailable():
    # Simulates an old config.json where GPU transitions were enabled and a
    # specific effect chosen, but this session's capability probe failed
    # (different machine, driver regression, etc.) -- the stored effect
    # name must not prevent a clean fallback to classic/Phase 1, and must
    # not raise.
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=False, video_dual_transitions_enabled=True,
        video_gpu_transition_effect="Zoom",
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with(None)
    assert window._configure_dual_calls[-1].enabled is False


# -- Phase 2C: new effects follow the same safety paths ---------------------

def test_apply_gpu_dual_mode_state_old_config_phase_2c_effect_falls_back_when_gpu_unavailable():
    # Same as the Phase 2B case above, but for a Phase 2C effect name --
    # confirms the fallback path is effect-name-agnostic, not something
    # that only happened to work for the original 6 effects.
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=False, video_dual_transitions_enabled=True,
        video_gpu_transition_effect="Film Burn",
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    window._video_backend.set_dual_mode.assert_called_once_with(None)
    assert window._configure_dual_calls[-1].enabled is False


def test_apply_gpu_dual_mode_state_passes_phase_2c_effect_through_when_available():
    window = _fake_window_for_gpu_gate(
        _gpu_dual_capability=True, video_dual_transitions_enabled=True,
        video_gpu_transition_effect="Diagonal Wipe",
    )
    PlayerWindow._apply_gpu_dual_mode_state(window)
    assert window._configure_dual_calls[-1].gpu_effect == "Diagonal Wipe"


def test_preferences_dialog_gpu_effect_combo_present_and_gated_with_checkbox():
    source = inspect.getsource(PlayerWindow._show_normalisation_preferences)
    assert "GPU_TRANSITION_STYLES" in source
    assert "video_gpu_transition_effect" in source
    assert "gpu_dual_available and video_dual_transitions_enabled.isChecked()" in source


def test_save_user_settings_persists_gpu_transition_effect():
    source = inspect.getsource(PlayerWindow._save_user_settings)
    assert 'cfg["video_gpu_transition_effect"]' in source


def test_start_gpu_capability_probe_is_skipped_when_already_resolved():
    from billsmusic.video_backend import GpuCompositorProbe
    window = _fake_window_for_gpu_gate(_gpu_dual_capability=True)
    PlayerWindow._start_gpu_capability_probe(window)
    assert window._gpu_dual_probe is None  # never launched -- already known


def test_start_gpu_capability_probe_is_skipped_when_already_in_flight():
    in_flight_probe = object()
    window = _fake_window_for_gpu_gate(_gpu_dual_probe=in_flight_probe)
    PlayerWindow._start_gpu_capability_probe(window)
    assert window._gpu_dual_probe is in_flight_probe  # unchanged, not replaced


def test_gpu_capability_probe_finished_caches_result_and_applies_state():
    applied = []
    window = _fake_window_for_gpu_gate(_gpu_dual_probe=object())
    window._apply_gpu_dual_mode_state = lambda: applied.append(True)
    PlayerWindow._on_gpu_capability_probe_finished(window, True, "")
    assert window._gpu_dual_capability is True
    assert window._gpu_dual_probe is None
    assert applied == [True]


def test_gpu_capability_probe_finished_caches_failure():
    window = _fake_window_for_gpu_gate(_gpu_dual_probe=object())
    window._apply_gpu_dual_mode_state = lambda: None
    PlayerWindow._on_gpu_capability_probe_finished(window, False, "non_gpu_backend:Software")
    assert window._gpu_dual_capability is False
    assert window._gpu_dual_probe is None
