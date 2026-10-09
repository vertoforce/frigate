"""Peewee migrations -- 036_local_create_detection_mark_table.py.

LOCAL FORK (not upstream). Stores the user's real / false verdicts on tracked
object detections, used to fine-tune our own detector. One row per event.
See /root/media/frigate/docs/false-positive-finetune-status.md on the host.
"""

import peewee as pw

SQL = pw.SQL


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        """
        CREATE TABLE IF NOT EXISTS detection_mark (
            event_id VARCHAR(30) NOT NULL PRIMARY KEY,
            camera VARCHAR(20) NOT NULL,
            label VARCHAR(20) NOT NULL,
            verdict VARCHAR(10) NOT NULL,
            box TEXT,
            region TEXT,
            frame_time REAL,
            frame_path TEXT,
            retain_before INTEGER NOT NULL DEFAULT 0,
            marked_at REAL NOT NULL
        )
        """
    )
    migrator.sql(
        "CREATE INDEX IF NOT EXISTS detection_mark_camera ON detection_mark (camera)"
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql("DROP TABLE IF EXISTS detection_mark")
