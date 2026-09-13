import inspect

from billsmusic.window import PlayerWindow


def test_shutdown_does_not_reference_undefined_crossfade_argument():
    # Phase C2 (2026-09-11): crossfade-token invalidation lives in
    # _request_shutdown() now (the former single _shutdown_threads() was
    # split into _request_shutdown()/_finalize_shutdown()).
    source = inspect.getsource(PlayerWindow._request_shutdown)

    assert "if crossfade" not in source
