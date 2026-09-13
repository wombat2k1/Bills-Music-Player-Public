import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from billsmusic.session import (
    SessionError,
    clear_session_file,
    load_session_file,
    save_session_file,
    session_document,
)


class SessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary_directory.name)
        self.session_path = self.folder / "session.json"
        self.first = self.folder / "first.mp3"
        self.second = self.folder / "second.mp3"
        self.first.touch()
        self.second.touch()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_saves_and_loads_unplayed_queue_in_order(self):
        queue = [str(self.second), str(self.first)]
        save_session_file(str(self.session_path), queue, [False, False], str(self.first))
        self.assertEqual(
            load_session_file(str(self.session_path)),
            (queue, [False, False], str(self.first)),
        )

    def test_preserves_played_flags(self):
        queue = [str(self.first), str(self.second)]
        save_session_file(str(self.session_path), queue, [False, True], None)
        self.assertEqual(load_session_file(str(self.session_path)), (queue, [False, True], None))

    def test_ignores_missing_and_invalid_paths(self):
        missing = self.folder / "missing.mp3"
        self.session_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queue": [
                        {"path": str(missing), "played": True},
                        {"path": 123, "played": True},
                        {"path": str(self.first), "played": False},
                    ],
                    "current_path": str(missing),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(load_session_file(str(self.session_path)), ([str(self.first)], [False], None))

    def test_invalid_or_missing_played_values_become_false(self):
        self.session_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queue": [
                        {"path": str(self.first), "played": "yes"},
                        {"path": str(self.second)},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_session_file(str(self.session_path)),
            ([str(self.first), str(self.second)], [False, False], None),
        )

    def test_malformed_json_is_safe_error(self):
        self.session_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SessionError):
            load_session_file(str(self.session_path))

    def test_unsupported_version_is_safe_error(self):
        self.session_path.write_text(json.dumps({"version": 999, "queue": []}), encoding="utf-8")
        with self.assertRaises(SessionError):
            load_session_file(str(self.session_path))

    def test_empty_queue_round_trip(self):
        save_session_file(str(self.session_path), [], [], None)
        self.assertEqual(load_session_file(str(self.session_path)), ([], [], None))

    def test_atomic_save_leaves_no_temporary_file(self):
        save_session_file(str(self.session_path), [str(self.first)], [False], None)
        self.assertTrue(self.session_path.exists())
        self.assertFalse(Path(str(self.session_path) + ".tmp").exists())

    def test_atomic_failure_cleans_temporary_file(self):
        with mock.patch("billsmusic.session.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                save_session_file(str(self.session_path), [str(self.first)], [False], None)
        self.assertFalse(Path(str(self.session_path) + ".tmp").exists())

    def test_clear_saved_session(self):
        save_session_file(str(self.session_path), [], [], None)
        clear_session_file(str(self.session_path))
        clear_session_file(str(self.session_path))
        self.assertFalse(self.session_path.exists())

    def test_mismatched_flags_are_normalized_to_queue_length(self):
        document = session_document(
            [str(self.first), str(self.second)], [True], None
        )
        self.assertEqual([entry["played"] for entry in document["queue"]], [True, False])

    def test_loading_has_no_playback_side_effect(self):
        save_session_file(str(self.session_path), [str(self.first)], [False], str(self.first))
        with mock.patch("billsmusic.session.os.path.isfile", wraps=os.path.isfile) as isfile:
            restored = load_session_file(str(self.session_path))
        self.assertEqual(restored, ([str(self.first)], [False], str(self.first)))
        self.assertGreaterEqual(isfile.call_count, 2)

    def test_startup_restore_can_skip_slow_network_path_checks(self):
        missing = self.folder / "network-track.mp3"
        save_session_file(
            str(self.session_path), [str(missing)], [False], str(missing)
        )
        with mock.patch("billsmusic.session.os.path.isfile") as isfile:
            restored = load_session_file(
                str(self.session_path), validate_paths=False
            )
        self.assertEqual(
            restored, ([str(missing)], [False], str(missing))
        )
        isfile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
