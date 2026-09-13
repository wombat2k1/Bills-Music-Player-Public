from types import SimpleNamespace

import billsmusic.workers as workers


def test_queue_worker_reads_time_and_bitrate_off_gui_thread(monkeypatch):
    audio = SimpleNamespace(
        info=SimpleNamespace(length=247.2, bitrate=1_411_200)
    )
    monkeypatch.setattr(workers, "MutagenFile", lambda path, easy=True: audio)
    monkeypatch.setattr(workers, "np", None)

    result = workers.QueueAnalysisWorker()._analyse_track("network.flac")

    assert result["time"] == "4:07"
    assert result["bitrate"] == "1411k"
