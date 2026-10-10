"""Tests for the local motion padding retention in recording cleanup."""

import unittest
from unittest.mock import MagicMock, patch

from peewee import SqliteDatabase

from frigate.models import Previews, Recordings
from frigate.record.cleanup import RecordingCleanup


class TestCleanupMotionPadding(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.db.bind([Recordings, Previews])
        self.db.create_tables([Recordings, Previews])

    def tearDown(self):
        self.db.close()

    def _add(self, rec_id: str, start: float, motion: int) -> None:
        Recordings.insert(
            id=rec_id,
            camera="cam",
            path=f"/media/frigate/recordings/{rec_id}.mp4",
            start_time=start,
            end_time=start + 10,
            duration=10,
            motion=motion,
            objects=0,
            dBFS=0,
        ).execute()

    def test_keeps_quiet_segments_next_to_motion_only(self):
        self._add("motion", 1000, motion=5)
        self._add("pad_after", 1010, motion=0)
        self._add("pad_before", 985, motion=0)
        self._add("far", 1100, motion=0)

        config = MagicMock()
        config.name = "cam"
        config.record.motion.days = 1
        cleanup = RecordingCleanup.__new__(RecordingCleanup)

        with patch("frigate.record.cleanup._delete_file", return_value=None):
            cleanup.expire_existing_camera_recordings(
                continuous_expire_date=5000,
                motion_expire_date=0,
                config=config,
                reviews=[],
            )

        remaining = {r.id for r in Recordings.select(Recordings.id)}
        self.assertEqual({"motion", "pad_after", "pad_before"}, remaining)

    def test_padding_expires_with_motion_retention(self):
        self._add("motion", 1000, motion=5)
        self._add("pad_after", 1010, motion=0)

        config = MagicMock()
        config.name = "cam"
        config.record.motion.days = 1
        cleanup = RecordingCleanup.__new__(RecordingCleanup)

        with patch("frigate.record.cleanup._delete_file", return_value=None):
            cleanup.expire_existing_camera_recordings(
                continuous_expire_date=5000,
                motion_expire_date=5000,
                config=config,
                reviews=[],
            )

        self.assertEqual(0, Recordings.select().count())


if __name__ == "__main__":
    unittest.main()
