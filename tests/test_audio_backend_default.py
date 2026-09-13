from billsmusic.window import (
    AUDIO_BACKEND_PREFERENCE_VERSION,
    resolve_audio_backend_preference,
)


def test_existing_preference_is_migrated_once_to_bass():
    backend, use_builtin = resolve_audio_backend_preference({
        "audio_backend": "miniaudio",
        "use_simple_player": False,
    })

    assert backend == "bass"
    assert use_builtin is True


def test_explicit_choice_is_preserved_after_bass_default_migration():
    backend, use_builtin = resolve_audio_backend_preference({
        "audio_backend_preference_version": AUDIO_BACKEND_PREFERENCE_VERSION,
        "audio_backend": "miniaudio",
        "use_simple_player": False,
    })

    assert backend == "miniaudio"
    assert use_builtin is False


def test_new_install_defaults_to_bass():
    assert resolve_audio_backend_preference({}) == ("bass", True)
