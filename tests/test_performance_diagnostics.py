import json
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

from billsmusic.performance_diagnostics import (
    MAX_PENDING_DIAGNOSTIC_EVENTS,
    RECENT_EVENT_LIMIT,
    PerformanceDiagnostics,
    redact_configuration,
)


def collector(tmp_path, level="developer", start_writer=False):
    return PerformanceDiagnostics(
        str(tmp_path), level=level, start_writer=start_writer,
        gui_thread_id=threading.get_ident(),
    )


def test_structured_event_has_schema_identifiers_and_thread(tmp_path):
    diagnostics = collector(tmp_path)
    event = diagnostics.record("library", "restore", generation=42)
    assert event["schema_version"] == 1
    assert event["session_id"] and event["event_id"]
    assert event["generation"] == 42
    assert event["thread_name"] == threading.current_thread().name
    assert event["gui_thread"] is True


def test_writer_serialises_json_lines(tmp_path):
    diagnostics = collector(tmp_path, start_writer=True)
    diagnostics.record("startup", "test_event")
    diagnostics.flush()
    diagnostics.shutdown()
    lines = diagnostics.log_path.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["operation"] == "test_event" for line in lines)


def test_measure_records_success_and_duration(tmp_path):
    diagnostics = collector(tmp_path)
    with diagnostics.measure("gui", "refresh"):
        time.sleep(0.001)
    event = diagnostics.recent_events[-1]
    assert event["status"] == "success"
    assert event["phase"] == "complete"
    assert event["duration_ms"] >= 1


def test_measure_records_failure_and_reraises(tmp_path):
    diagnostics = collector(tmp_path)
    with pytest.raises(ValueError):
        with diagnostics.measure("library", "explode"):
            raise ValueError("broken")
    event = diagnostics.recent_events[-1]
    assert event["status"] == "failure"
    assert event["details"]["exception_type"] == "ValueError"


def test_measure_records_cancellation_separately(tmp_path):
    diagnostics = collector(tmp_path)
    with diagnostics.measure("search", "cancelled") as operation:
        operation.cancel("new generation")
    assert diagnostics.recent_events[-1]["status"] == "cancelled"


def test_ring_buffer_and_pending_queue_are_bounded(tmp_path):
    diagnostics = collector(tmp_path)
    for index in range(MAX_PENDING_DIAGNOSTIC_EVENTS + 250):
        diagnostics.record("worker", "sample", details={"index": index})
    assert len(diagnostics.recent_events) == RECENT_EVENT_LIMIT
    assert diagnostics.pending.qsize() <= MAX_PENDING_DIAGNOSTIC_EVENTS
    assert sum(diagnostics.dropped_events.values()) > 0


def test_severe_event_is_retained_when_queue_is_full(tmp_path):
    diagnostics = collector(tmp_path)
    for index in range(MAX_PENDING_DIAGNOSTIC_EVENTS):
        diagnostics.record("worker", "low", details={"index": index})
    event = diagnostics.record(
        "event_loop", "freeze", severity="severe", minimum_level="basic"
    )
    queued = [diagnostics.pending.get_nowait()[1] for _ in range(diagnostics.pending.qsize())]
    assert any(item.get("event_id") == event["event_id"] for item in queued)
    assert diagnostics.dropped_events["low_priority"] >= 1


def test_worker_wait_and_execution_are_separate(tmp_path):
    diagnostics = collector(tmp_path)
    token = diagnostics.submit_worker("worker", "metadata_read")
    token.submitted_at -= 0.01
    with diagnostics.run_worker(token):
        time.sleep(0.001)
    event = diagnostics.recent_events[-1]
    assert event["queue_wait_ms"] >= 10
    assert event["duration_ms"] >= 1
    assert diagnostics.worker_stats["metadata_read"]["completed"] == 1


def test_event_loop_thresholds_and_rate_limiting(tmp_path):
    diagnostics = collector(tmp_path, level="detailed")
    diagnostics.observe_event_loop(175, {"playing": True})
    diagnostics.observe_event_loop(180, {"playing": True})
    warnings = [
        event for event in diagnostics.recent_events
        if event["operation"] == "gui_lag"
    ]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "severe"
    assert diagnostics.lag_counts["150"] == 2
    assert diagnostics.counters["rate_limited"] == 1


def test_visualiser_metrics_are_aggregated_and_hidden_is_not_stalled(tmp_path):
    diagnostics = collector(tmp_path)
    for _ in range(20):
        diagnostics.observe_visual_frame(2, 16, visible=True)
    diagnostics.observe_visual_frame(100, 500, visible=False)
    assert diagnostics.visual_stats["frames_painted"] == 20
    assert diagnostics.visual_stats["hidden_samples"] == 1
    assert not any(
        event["operation"] == "frame_stutter"
        for event in diagnostics.recent_events
    )


def test_visual_frame_defaults_to_main_consumer_when_unspecified(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.observe_visual_frame(2, 16, visible=True)
    assert diagnostics.visual_stats_by_consumer["main"]["frames_painted"] == 1
    assert diagnostics.visual_stats_by_consumer["party_mode"]["frames_painted"] == 0


def test_visual_frame_tracks_each_consumer_independently(tmp_path):
    diagnostics = collector(tmp_path)
    for _ in range(10):
        diagnostics.observe_visual_frame(1.0, 16.0, visible=True, consumer="main")
    for _ in range(4):
        diagnostics.observe_visual_frame(45.0, 60.0, visible=True, consumer="party_mode")

    main_stats = diagnostics.visual_stats_by_consumer["main"]
    party_stats = diagnostics.visual_stats_by_consumer["party_mode"]
    assert main_stats["frames_painted"] == 10
    assert party_stats["frames_painted"] == 4
    assert diagnostics.visual_max_by_consumer["party_mode"]["paint_ms"] == 45.0
    assert diagnostics.visual_max_by_consumer["main"]["paint_ms"] == 1.0
    # The shared aggregate counters must still reflect everything combined.
    assert diagnostics.visual_stats["frames_painted"] == 14


def test_visual_frame_hidden_samples_are_not_attributed_to_any_consumer(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.observe_visual_frame(100, 500, visible=False, consumer="party_mode")
    assert diagnostics.visual_stats_by_consumer["party_mode"]["frames_painted"] == 0


def test_frame_stutter_event_is_tagged_with_its_consumer(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.observe_visual_frame(45.0, 20.0, visible=True, consumer="party_mode")
    stutters = [
        event for event in diagnostics.recent_events
        if event["operation"] == "frame_stutter"
    ]
    assert len(stutters) == 1
    assert stutters[0]["details"]["consumer"] == "party_mode"


def test_summary_dict_breaks_down_visualiser_stats_by_consumer(tmp_path):
    diagnostics = collector(tmp_path)
    for _ in range(10):
        diagnostics.observe_visual_frame(1.0, 16.0, visible=True, consumer="main")
    for _ in range(4):
        diagnostics.observe_visual_frame(45.0, 60.0, visible=True, consumer="party_mode")

    summary = diagnostics.summary_dict()
    by_consumer = summary["visualiser"]["by_consumer"]
    assert by_consumer["main"]["frames_painted"] == 10
    assert by_consumer["main"]["average_paint_ms"] == 1.0
    assert by_consumer["party_mode"]["frames_painted"] == 4
    assert by_consumer["party_mode"]["average_paint_ms"] == 45.0
    assert by_consumer["party_mode"]["longest_gap_ms"] == 60.0


def test_rotation_and_incident_retention_are_bounded(tmp_path):
    diagnostics = collector(tmp_path)
    path = tmp_path / "small.jsonl"
    path.write_text("x" * 20, encoding="utf-8")
    for _ in range(5):
        diagnostics._rotate(path, 10, 2)
        path.write_text("x" * 20, encoding="utf-8")
    assert Path(str(path) + ".1").exists()
    assert Path(str(path) + ".2").exists()
    assert not Path(str(path) + ".3").exists()

    incident_dir = tmp_path / "incidents"
    incident_dir.mkdir()
    for index in range(20):
        item = incident_dir / f"incident-{index:02}.json"
        item.write_text("{}", encoding="utf-8")
        os.utime(item, (index, index))
    diagnostics._retain(incident_dir.glob("incident-*.json"), 15)
    assert len(list(incident_dir.glob("incident-*.json"))) == 15


def test_summary_includes_lag_memory_workers_and_overhead(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.observe_event_loop(75)
    diagnostics.peak_memory_bytes = 100 * 1024 * 1024
    token = diagnostics.submit_worker("worker", "one")
    with diagnostics.run_worker(token):
        pass
    summary = diagnostics.summary_dict()
    assert summary["event_loop"]["longest_lag_ms"] == 75
    assert summary["peak_memory_bytes"] == 100 * 1024 * 1024
    assert "one" in summary["workers"]
    assert "average_event_build_us" in summary["diagnostics_overhead"]


def test_redaction_removes_keys_tokens_and_passwords():
    redacted = redact_configuration({
        "audio_backend": "bass",
        "diagnostics_level": "detailed",
        "api_key": "secret-value",
        "password": "do-not-copy",
    })
    text = json.dumps(redacted)
    assert "secret-value" not in text
    assert "do-not-copy" not in text
    assert redacted["audio_backend"] == "bass"


def test_paths_are_hashed_by_default_and_optional_when_enabled(tmp_path):
    path = r"Z:\Private\Music\Artist\song.flac"
    diagnostics = collector(tmp_path)
    hidden = diagnostics.path_details(path)
    assert hidden["extension"] == ".flac"
    assert "path" not in hidden
    assert "Private" not in json.dumps(hidden)
    diagnostics.configure("developer", True)
    assert diagnostics.path_details(path)["path"] == path


def test_export_bundle_contains_safe_files_and_excludes_music_secrets(tmp_path):
    diagnostics = collector(tmp_path, start_writer=True)
    diagnostics.record("audio", "recovery", minimum_level="basic")
    diagnostics.write_session_summary()
    music = tmp_path / "private-song.flac"
    music.write_bytes(b"music")
    player_logs = tmp_path / "logs"
    player_logs.mkdir()
    (player_logs / "player.log").write_text(
        r"token=super-secret opened Z:\Private\Music\Artist\song.flac",
        encoding="utf-8",
    )
    destination = tmp_path / "bundle.zip"
    diagnostics.export_bundle(
        str(destination),
        config={
            "diagnostics_level": "developer",
            "audio_backend": "bass",
            "api_key": "super-secret",
        },
        application_details={"title": "Bills Music Player"},
        player_log_directory=str(player_logs),
    )
    diagnostics.shutdown()
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        content = "\n".join(
            archive.read(name).decode("utf-8", "ignore")
            for name in names
            if name.endswith((".txt", ".json", ".jsonl", ".log"))
        )
    assert "README.txt" in names
    assert "diagnostic_manifest.json" in names
    assert not any(name.endswith(".flac") for name in names)
    assert "super-secret" not in content
    assert r"Z:\Private\Music" not in content
    assert "<path:" in content


def test_unwritable_output_and_malformed_details_never_raise(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")
    diagnostics = PerformanceDiagnostics(
        str(blocked), level="developer", start_writer=False
    )
    assert diagnostics._file_output is False
    event = diagnostics.record(
        "exception", "odd_details", details={"value": object()}
    )
    assert event is not None


def test_off_level_disables_normal_events_but_keeps_fatal(tmp_path):
    diagnostics = collector(tmp_path, level="off")
    assert diagnostics.record("gui", "normal") is None
    assert diagnostics.record(
        "exception", "fatal", severity="fatal"
    ) is not None


def test_basic_detailed_and_developer_levels_differ(tmp_path):
    basic = collector(tmp_path / "basic", level="basic")
    assert basic.record(
        "search", "normal_detail", minimum_level="detailed"
    ) is None
    assert basic.record(
        "event_loop", "freeze", severity="severe",
        minimum_level="basic",
    ) is not None

    detailed = collector(tmp_path / "detailed", level="detailed")
    assert detailed.record(
        "search", "normal_detail", minimum_level="detailed"
    ) is not None

    developer = collector(tmp_path / "developer", level="developer")
    developer.submit_worker("worker", "developer_task")
    assert any(
        event["phase"] == "submitted"
        for event in developer.recent_events
    )


def test_summary_ignores_zero_duration_and_reports_slowest_gui_operation(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.record(
        "search", "placeholder", duration_ms=0, minimum_level="basic"
    )
    diagnostics.record(
        "library", "completed_apply", duration_ms=42, minimum_level="basic"
    )
    summary = diagnostics.summary_dict()
    assert summary["slowest_operation"]["operation"] == "completed_apply"
    assert summary["slowest_operation"]["duration_ms"] == 42
    assert summary["slowest_gui_operation"]["operation"] == "completed_apply"
    diagnostics.shutdown()


def test_unavailable_memory_is_not_reported_as_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        PerformanceDiagnostics, "_working_set_bytes", staticmethod(lambda: None)
    )
    diagnostics = collector(tmp_path)
    diagnostics.sample_memory()
    summary = diagnostics.summary_dict()
    assert summary["peak_memory_bytes"] is None
    assert summary["memory_sampling"]["available"] is False
    assert "unavailable" in diagnostics.human_summary()
    diagnostics.shutdown()


def test_windows_working_set_sampler_returns_current_process_memory():
    if os.name != "nt":
        pytest.skip("Windows-specific working-set API")

    working_set = PerformanceDiagnostics._working_set_bytes()

    assert isinstance(working_set, int)
    assert working_set > 0


# ---------------------------------------------------------------------------
# Phase C2 (worker lifetime / shutdown ownership, 2026-09-11): record()
# must become a no-op once shutdown() has completed, since the writer
# thread that would drain self.pending is already gone by then.
# ---------------------------------------------------------------------------

def test_record_after_shutdown_is_a_noop_and_never_queues(tmp_path):
    diagnostics = collector(tmp_path)  # start_writer=False -- pending is never drained
    diagnostics.shutdown()
    before = diagnostics.pending.qsize()
    result = diagnostics.record("playback", "late_event_after_shutdown")
    assert result is None
    assert diagnostics.pending.qsize() == before


def test_shutdown_records_its_own_final_event_before_the_guard_takes_effect(tmp_path):
    # The guard must not swallow shutdown()'s own completion event --
    # record() is called from inside shutdown() itself, before
    # _shutdown_complete is set.
    diagnostics = collector(tmp_path)
    diagnostics.shutdown()
    ops = [e["operation"] for e in diagnostics.recent_events]
    assert "diagnostics_session" in ops


def test_shutdown_is_idempotent_and_the_second_call_records_nothing_new(tmp_path):
    diagnostics = collector(tmp_path)
    diagnostics.shutdown()
    count_after_first = len(diagnostics.recent_events)
    diagnostics.shutdown()  # must not raise, must not re-record
    assert len(diagnostics.recent_events) == count_after_first
