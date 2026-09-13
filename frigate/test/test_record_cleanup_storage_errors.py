"""Tests that a storage error cannot end recording cleanup.

RecordingCleanup runs as an unsupervised thread, so an OSError raised while
deleting one expired file (a read-only mount, a stale network handle) used to
propagate out of run() and stop retention for the life of the process.

0.18 has no Recordings.stream_type column, so the rows here omit it; the
cleanup entry points and the seven delete sites are otherwise the same.
"""

import datetime
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.config import FrigateConfig
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup

CLEANUP_LOGGER = "frigate.record.cleanup"


def read_only_error() -> OSError:
    return OSError(errno.EROFS, "Read-only file system")


class TestRecordingCleanupStorageErrors(unittest.TestCase):
    def setUp(self):
        # in-memory database keeps these tests isolated from the shared
        # on-disk test.db used by the http api tests
        self.db = SqliteExtDatabase(":memory:")
        models = [Previews, Recordings, ReviewSegment, UserReviewStatus]
        self.db.bind(models)
        self.db.create_tables(models)

    def tearDown(self):
        self.db.close()

    def _build_cleanup(self) -> RecordingCleanup:
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "cameras": {
                    "front_door": {
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": "rtsp://10.0.0.1:554/video",
                                    "roles": ["detect", "record"],
                                }
                            ]
                        },
                        "record": {"enabled": True, "continuous": {"days": 7}},
                    }
                },
            }
        )
        return RecordingCleanup(config, MagicMock())

    def _ago(self, days: float) -> float:
        return (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()

    def _insert_recording(self, id: str, age_days: float, camera: str) -> None:
        end_time = self._ago(age_days)
        Recordings.create(
            id=id,
            camera=camera,
            path=f"/media/frigate/recordings/{camera}/{id}.mp4",
            start_time=end_time - 10,
            end_time=end_time,
            duration=10,
            motion=0,
            objects=0,
            dBFS=0,
            segment_size=0,
        )

    def _insert_preview(self, id: str, age_days: float) -> None:
        end_time = self._ago(age_days)
        Previews.create(
            id=id,
            camera="front_door",
            path=f"/media/frigate/previews/{id}.mp4",
            start_time=end_time - 10,
            end_time=end_time,
            duration=10,
        )

    def _insert_review(self, id: str, age_days: float) -> None:
        end_time = self._ago(age_days)
        ReviewSegment.create(
            id=id,
            camera="front_door",
            start_time=end_time - 10,
            end_time=end_time,
            severity="alert",
            thumb_path=f"/media/frigate/clips/review/{id}.webp",
            data={},
        )

    def _insert_expired_rows(self) -> None:
        # one row per unlink site in cleanup.py: an expired recording for a
        # configured camera, an expired recording for a camera that is no
        # longer in the config, an expired preview and an expired review
        self._insert_recording("m1", 30, "front_door")
        self._insert_recording("gone1", 30, "removed_camera")
        self._insert_preview("p1", 30)
        self._insert_review("r1", 30)

    def test_expire_recordings_deletes_rows_when_storage_is_writable(self):
        """Baseline: nothing here changes the normal, writable path."""
        cleanup = self._build_cleanup()
        self._insert_expired_rows()

        cleanup.expire_recordings()

        assert Recordings.get_or_none(Recordings.id == "m1") is None
        assert Recordings.get_or_none(Recordings.id == "gone1") is None
        assert Previews.get_or_none(Previews.id == "p1") is None
        assert ReviewSegment.get_or_none(ReviewSegment.id == "r1") is None

    def test_expire_recordings_survives_read_only_storage(self):
        """An EROFS unlink must not propagate out of the cleanup pass."""
        cleanup = self._build_cleanup()
        self._insert_expired_rows()

        with patch.object(Path, "unlink", side_effect=read_only_error()):
            with self.assertLogs(CLEANUP_LOGGER, level="WARNING") as logs:
                cleanup.expire_recordings()

        assert any("Read-only file system" in line for line in logs.output), logs.output

        # the files are still on disk, so their rows are kept and the next
        # pass retries them
        assert Recordings.get_or_none(Recordings.id == "m1") is not None
        assert Recordings.get_or_none(Recordings.id == "gone1") is not None
        assert Previews.get_or_none(Previews.id == "p1") is not None
        assert ReviewSegment.get_or_none(ReviewSegment.id == "r1") is not None

    def test_cleanup_cycle_survives_read_only_directory_prune(self):
        """An EROFS rmdir must not propagate out of run()."""
        cleanup = self._build_cleanup()
        # one cycle, then stop
        cleanup.stop_event.wait.side_effect = [False, True]

        empty_dir = Path("/media/frigate/recordings/2026-09-13/10/front_door")

        with (
            patch.object(RecordingCleanup, "clean_tmp_previews"),
            patch.object(RecordingCleanup, "clean_tmp_clips"),
            patch.object(RecordingCleanup, "truncate_wal"),
            patch.object(
                RecordingCleanup, "expire_recordings", return_value={empty_dir}
            ),
            patch.object(Path, "rmdir", side_effect=read_only_error()),
        ):
            with self.assertLogs(CLEANUP_LOGGER, level="WARNING") as logs:
                cleanup.run()

        assert any("Read-only file system" in line for line in logs.output), logs.output

    def test_tmp_clip_cleanup_survives_read_only_storage(self):
        """An EROFS unlink in the hourly tmp clip sweep must not propagate."""
        cleanup = self._build_cleanup()

        with tempfile.TemporaryDirectory() as clips_dir:
            cache_dir = Path(clips_dir) / "cache"
            cache_dir.mkdir()
            stale_clip = cache_dir / "clip_front_door.mp4"
            stale_clip.touch()
            two_hours_ago = datetime.datetime.now().timestamp() - 2 * 60 * 60
            os.utime(stale_clip, (two_hours_ago, two_hours_ago))

            with (
                patch("frigate.record.cleanup.CLIPS_DIR", clips_dir),
                patch(
                    "frigate.record.cleanup.clear_and_unlink",
                    side_effect=read_only_error(),
                ),
            ):
                with self.assertLogs(CLEANUP_LOGGER, level="WARNING") as logs:
                    cleanup.clean_tmp_clips()

        assert any("Read-only file system" in line for line in logs.output), logs.output

    def test_tmp_preview_cleanup_survives_read_only_storage(self):
        """An EROFS unlink in the per-minute tmp preview sweep must not propagate."""
        cleanup = self._build_cleanup()

        with tempfile.TemporaryDirectory() as cache_dir:
            stale_preview = Path(cache_dir) / "preview_front_door.mp4"
            stale_preview.touch()
            two_hours_ago = datetime.datetime.now().timestamp() - 2 * 60 * 60
            os.utime(stale_preview, (two_hours_ago, two_hours_ago))

            with (
                patch("frigate.record.cleanup.CACHE_DIR", cache_dir),
                patch(
                    "frigate.record.cleanup.clear_and_unlink",
                    side_effect=read_only_error(),
                ),
            ):
                with self.assertLogs(CLEANUP_LOGGER, level="WARNING") as logs:
                    cleanup.clean_tmp_previews()

        assert any("Read-only file system" in line for line in logs.output), logs.output
