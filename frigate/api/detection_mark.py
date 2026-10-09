"""LOCAL FORK: mark tracked object detections as real or false.

The marks are training data for fine-tuning our own detector
(/root/media/frigate/tools/finetune_detector.py on the host). At mark time the
event is set to retain indefinitely and the clean snapshot frame is copied to
MARK_FRAME_DIR, so retention never deletes the training frame.
"""

import datetime
import logging
import os
import shutil

from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from peewee import DoesNotExist, fn
from pydantic import BaseModel

from frigate.api.auth import require_camera_access, require_role
from frigate.api.defs.tags import Tags
from frigate.const import BASE_DIR
from frigate.models import DetectionMark, Event
from frigate.util.file import get_event_snapshot_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=[Tags.events])

MARK_FRAME_DIR = os.path.join(BASE_DIR, "finetune-marks")
VERDICTS = ("real", "false")


class DetectionMarkBody(BaseModel):
    verdict: str


def _mark_dict(mark: DetectionMark) -> dict:
    return {
        "event_id": mark.event_id,
        "camera": mark.camera,
        "label": mark.label,
        "verdict": mark.verdict,
        "box": mark.box,
        "region": mark.region,
        "frame_time": mark.frame_time,
        "frame_path": mark.frame_path,
        "marked_at": mark.marked_at,
    }


def _summary() -> dict:
    counts = {"real": 0, "false": 0}
    for row in (
        DetectionMark.select(DetectionMark.verdict, fn.COUNT(DetectionMark.event_id))
        .group_by(DetectionMark.verdict)
        .tuples()
    ):
        counts[row[0]] = row[1]
    counts["total"] = counts["real"] + counts["false"]
    return counts


@router.get(
    "/detection_marks/summary",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Count of real / false detection marks (local fork).",
)
def detection_marks_summary():
    return JSONResponse(content=_summary())


@router.get(
    "/detection_marks",
    dependencies=[Depends(require_role(["admin"]))],
    summary="List detection marks (local fork).",
)
def detection_marks_list(limit: int = 1000):
    marks = (
        DetectionMark.select()
        .order_by(DetectionMark.marked_at.desc())
        .limit(max(1, min(limit, 100000)))
    )
    return JSONResponse(content=[_mark_dict(m) for m in marks])


@router.get(
    "/events/{event_id}/mark",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Get the detection mark for one event (local fork).",
)
def get_detection_mark(event_id: str):
    try:
        mark = DetectionMark.get(DetectionMark.event_id == event_id)
    except DoesNotExist:
        return JSONResponse(content={"verdict": None, "summary": _summary()})
    return JSONResponse(content={**_mark_dict(mark), "summary": _summary()})


@router.post(
    "/events/{event_id}/mark",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Mark an event's detection as real or false (local fork).",
    description="""Body {"verdict": "real" | "false"}. Upserts one row per event in
    the detection_mark table, sets the event to retain indefinitely and copies
    the clean snapshot frame out of retention's reach.""",
)
async def set_detection_mark(request: Request, event_id: str, body: DetectionMarkBody):
    verdict = body.verdict.strip().lower()
    if verdict not in VERDICTS:
        return JSONResponse(
            content={"success": False, "message": "verdict must be real or false"},
            status_code=400,
        )

    try:
        event: Event = Event.get(Event.id == event_id)
    except DoesNotExist:
        return JSONResponse(
            content={"success": False, "message": f"Event {event_id} not found"},
            status_code=404,
        )

    await require_camera_access(event.camera, request=request)

    data = event.data or {}
    existing = DetectionMark.get_or_none(DetectionMark.event_id == event_id)

    # Copy the clean frame once; it is the training image.
    frame_path = existing.frame_path if existing else None
    if not frame_path or not os.path.exists(frame_path):
        frame_path = None
        src, is_clean = get_event_snapshot_path(event, clean_only=True)
        if src and is_clean:
            try:
                os.makedirs(MARK_FRAME_DIR, exist_ok=True)
                ext = os.path.splitext(src)[1]
                frame_path = os.path.join(
                    MARK_FRAME_DIR, f"{event.camera}-{event.id}{ext}"
                )
                shutil.copy2(src, frame_path)
            except OSError as e:
                logger.error(f"detection mark: could not copy frame for {event_id}: {e}")
                frame_path = None
        else:
            logger.warning(f"detection mark: no clean snapshot for {event_id}")

    retain_before = (
        existing.retain_before if existing else bool(event.retain_indefinitely)
    )

    row = {
        DetectionMark.event_id: event.id,
        DetectionMark.camera: event.camera,
        DetectionMark.label: event.label,
        DetectionMark.verdict: verdict,
        DetectionMark.box: data.get("box"),
        DetectionMark.region: data.get("region"),
        DetectionMark.frame_time: data.get("snapshot_frame_time"),
        DetectionMark.frame_path: frame_path,
        DetectionMark.retain_before: retain_before,
        DetectionMark.marked_at: datetime.datetime.now().timestamp(),
    }
    DetectionMark.insert(row).on_conflict_replace().execute()

    if not event.retain_indefinitely:
        Event.update(retain_indefinitely=True).where(Event.id == event.id).execute()

    return JSONResponse(
        content={
            "success": True,
            "verdict": verdict,
            "frame_saved": frame_path is not None,
            "summary": _summary(),
        }
    )


@router.delete(
    "/events/{event_id}/mark",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Remove an event's detection mark (local fork).",
    description="Deletes the row and its frame copy, and restores the event's previous retain flag.",
)
def delete_detection_mark(event_id: str):
    mark = DetectionMark.get_or_none(DetectionMark.event_id == event_id)
    if mark is None:
        return JSONResponse(
            content={"success": False, "message": "No mark for this event"},
            status_code=404,
        )

    if mark.frame_path and os.path.exists(mark.frame_path):
        try:
            os.remove(mark.frame_path)
        except OSError as e:
            logger.warning(f"detection mark: could not remove {mark.frame_path}: {e}")

    if not mark.retain_before:
        Event.update(retain_indefinitely=False).where(Event.id == event_id).execute()

    mark.delete_instance()
    return JSONResponse(content={"success": True, "summary": _summary()})
