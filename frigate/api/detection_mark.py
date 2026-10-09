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
from peewee import JOIN, Case, DoesNotExist, fn
from pydantic import BaseModel, Field

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


class DetectionMarkBulkBody(BaseModel):
    event_ids: list[str] = Field(default_factory=list, max_length=5000)
    verdict: str  # "real", "false" or "clear"
    person_only: bool = True  # skip events whose label is not person


class DetectionMarkLookupBody(BaseModel):
    event_ids: list[str] = Field(default_factory=list, max_length=5000)


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


def _apply_mark(event: Event, verdict: str) -> bool:
    """Upsert one mark, hold retention, copy the clean frame. Returns frame_saved."""
    data = event.data or {}
    existing = DetectionMark.get_or_none(DetectionMark.event_id == event.id)

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
                logger.error(f"detection mark: could not copy frame for {event.id}: {e}")
                frame_path = None
        else:
            logger.warning(f"detection mark: no clean snapshot for {event.id}")

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

    return frame_path is not None


def _clear_mark(event_id: str) -> bool:
    """Delete the row and frame copy, restore the old retain flag. False if no mark."""
    mark = DetectionMark.get_or_none(DetectionMark.event_id == event_id)
    if mark is None:
        return False

    if mark.frame_path and os.path.exists(mark.frame_path):
        try:
            os.remove(mark.frame_path)
        except OSError as e:
            logger.warning(f"detection mark: could not remove {mark.frame_path}: {e}")

    if not mark.retain_before:
        Event.update(retain_indefinitely=False).where(Event.id == event_id).execute()

    mark.delete_instance()
    return True


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
    frame_saved = _apply_mark(event, verdict)

    return JSONResponse(
        content={
            "success": True,
            "verdict": verdict,
            "frame_saved": frame_saved,
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
    if not _clear_mark(event_id):
        return JSONResponse(
            content={"success": False, "message": "No mark for this event"},
            status_code=404,
        )
    return JSONResponse(content={"success": True, "summary": _summary()})


@router.post(
    "/detection_marks/bulk",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Mark or clear many events in one request (local fork).",
    description="""Body {"event_ids": [...], "verdict": "real" | "false" | "clear",
    "person_only": true}. Same effect per event as /events/{id}/mark.""",
)
async def bulk_detection_mark(request: Request, body: DetectionMarkBulkBody):
    verdict = body.verdict.strip().lower()
    if verdict not in VERDICTS + ("clear",):
        return JSONResponse(
            content={"success": False, "message": "verdict must be real, false or clear"},
            status_code=400,
        )

    ids = list(dict.fromkeys(body.event_ids))
    events = {e.id: e for e in Event.select().where(Event.id.in_(ids))} if ids else {}
    changed, skipped, missing, no_frame = [], [], [], []

    for event_id in ids:
        if verdict == "clear":
            if _clear_mark(event_id):
                changed.append(event_id)
            else:
                skipped.append(event_id)
            continue

        event = events.get(event_id)
        if event is None:
            missing.append(event_id)
            continue
        if body.person_only and event.label != "person":
            skipped.append(event_id)
            continue
        await require_camera_access(event.camera, request=request)
        if not _apply_mark(event, verdict):
            no_frame.append(event_id)
        changed.append(event_id)

    return JSONResponse(
        content={
            "success": True,
            "verdict": verdict,
            "changed": len(changed),
            "skipped": skipped,
            "missing": missing,
            "no_frame": no_frame,
            "summary": _summary(),
        }
    )


@router.get(
    "/detection_marks/candidates",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Person events for bulk review, unmarked first (local fork).",
)
def detection_mark_candidates(
    cameras: str = "all",
    after: float | None = None,
    before: float | None = None,
    marked: str = "all",  # all | unmarked | marked
    label: str = "person",
    limit: int = 300,
    offset: int = 0,
):
    clauses = [Event.label == label, Event.end_time.is_null(False)]
    if cameras and cameras != "all":
        clauses.append(Event.camera.in_([c for c in cameras.split(",") if c]))
    if after is not None:
        clauses.append(Event.start_time >= after)
    if before is not None:
        clauses.append(Event.start_time <= before)
    if marked == "unmarked":
        clauses.append(DetectionMark.verdict.is_null(True))
    elif marked == "marked":
        clauses.append(DetectionMark.verdict.is_null(False))

    base = (
        Event.select(
            Event.id,
            Event.camera,
            Event.start_time,
            Event.top_score,
            Event.data,
            DetectionMark.verdict.alias("verdict"),
        )
        .join(
            DetectionMark,
            JOIN.LEFT_OUTER,
            on=(Event.id == DetectionMark.event_id),
        )
        .where(*clauses)
    )
    total = base.count()
    rows = (
        base.order_by(
            Case(None, [(DetectionMark.verdict.is_null(True), 0)], 1),
            Event.start_time.desc(),
        )
        .limit(max(1, min(limit, 2000)))
        .offset(max(0, offset))
        .dicts()
    )
    items = [
        {
            "id": r["id"],
            "camera": r["camera"],
            "start_time": r["start_time"],
            "score": (r["data"] or {}).get("top_score") or r["top_score"],
            "verdict": r["verdict"],
        }
        for r in rows
    ]
    return JSONResponse(content={"items": items, "total": total, "summary": _summary()})


@router.post(
    "/detection_marks/lookup",
    dependencies=[Depends(require_role(["admin"]))],
    summary="Current verdicts for a list of events (local fork).",
    description="""Body {"event_ids": [...]}. Returns {"verdicts": {event_id: "real" | "false"}}
    for the marked ones; unmarked ids are absent. POST so long id lists fit.""",
)
def detection_mark_lookup(body: DetectionMarkLookupBody):
    ids = list(dict.fromkeys(body.event_ids))
    verdicts = {}
    for i in range(0, len(ids), 500):  # stay under SQLite's variable limit
        for m in DetectionMark.select(
            DetectionMark.event_id, DetectionMark.verdict
        ).where(DetectionMark.event_id.in_(ids[i : i + 500])):
            verdicts[m.event_id] = m.verdict
    return JSONResponse(content={"verdicts": verdicts})
