from billsmusic.now_playing import NowPlayingGeneration


def test_authoritative_track_changes_get_monotonic_identities():
    guard = NowPlayingGeneration()

    first = guard.begin("A.mp3", 3)
    second = guard.begin("B.mp3", 4)

    assert first.generation == 1
    assert second.generation == 2
    assert second.queue_index == 4
    assert not guard.is_current(first.generation, "A.mp3")
    assert guard.is_current(second.generation, "B.mp3")


def test_same_path_replayed_later_is_not_the_old_queue_entry():
    guard = NowPlayingGeneration()
    first = guard.begin("duplicate.mp3", 1)
    replay = guard.begin("duplicate.mp3", 7)

    assert replay.generation == first.generation + 1
    assert not guard.is_current(first.generation, "duplicate.mp3")
    assert guard.is_current(replay.generation, "duplicate.mp3")


def test_non_track_operations_do_not_mutate_generation():
    guard = NowPlayingGeneration()
    identity = guard.begin("A.mp3", 0)

    # Pause/resume/seek/volume do not call begin(); validation itself is I/O-free.
    assert guard.is_current(identity.generation, "A.mp3")
    assert guard.identity == identity
