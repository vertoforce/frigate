// LOCAL FORK: per-event "Real person / Not a person / Clear mark" menu.
// Backend: POST /api/detection_marks/bulk (frigate/api/detection_mark.py).
import { ElementType, useCallback } from "react";
import axios from "axios";
import { toast } from "sonner";
import { LuMenu } from "react-icons/lu";
import { mutate } from "swr";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type MarkVerdict = "real" | "false" | "clear";

export type MarkSummary = { real: number; false: number; total: number };

export type BulkMarkResult = {
  changed: number;
  skipped: string[];
  missing: string[];
  no_frame: string[];
  summary: MarkSummary;
};

export async function markEvents(
  eventIds: string[],
  verdict: MarkVerdict,
): Promise<BulkMarkResult | undefined> {
  if (eventIds.length == 0) return undefined;
  try {
    const resp = await axios.post("detection_marks/bulk", {
      event_ids: eventIds,
      verdict,
      person_only: true,
    });
    const r: BulkMarkResult = resp.data;
    // keep the details-pane panel in sync
    eventIds.forEach((id) => mutate(`events/${id}/mark`));
    const what =
      verdict == "clear"
        ? "Cleared"
        : verdict == "real"
          ? "Marked real person:"
          : "Marked not a person:";
    toast.success(
      `${what} ${r.changed}` +
        (r.skipped.length ? ` (${r.skipped.length} skipped)` : "") +
        (r.no_frame.length ? `, ${r.no_frame.length} without a frame` : ""),
      { position: "top-center" },
    );
    return r;
  } catch (error) {
    toast.error("Could not save the marks", { position: "top-center" });
    return undefined;
  }
}

type DetectionMarkMenuItemsProps = {
  eventIds: string[];
  MenuItem: ElementType;
  onDone?: (r: BulkMarkResult | undefined) => void;
};

export function DetectionMarkMenuItems({
  eventIds,
  MenuItem,
  onDone,
}: DetectionMarkMenuItemsProps) {
  const run = useCallback(
    async (verdict: MarkVerdict) =>
      onDone?.(await markEvents(eventIds, verdict)),
    [eventIds, onDone],
  );
  return (
    <>
      <MenuItem
        className="cursor-pointer"
        onClick={(e: React.MouseEvent) => {
          e.stopPropagation();
          run("real");
        }}
      >
        <span>Real person</span>
      </MenuItem>
      <MenuItem
        className="cursor-pointer"
        onClick={(e: React.MouseEvent) => {
          e.stopPropagation();
          run("false");
        }}
      >
        <span>Not a person</span>
      </MenuItem>
      <MenuItem
        className="cursor-pointer"
        onClick={(e: React.MouseEvent) => {
          e.stopPropagation();
          run("clear");
        }}
      >
        <span>Clear mark</span>
      </MenuItem>
    </>
  );
}

type DetectionMarkMenuProps = {
  eventIds: string[];
  className?: string;
  onDone?: (r: BulkMarkResult | undefined) => void;
};

// Small three-line button for review tiles.
export default function DetectionMarkMenu({
  eventIds,
  className,
  onDone,
}: DetectionMarkMenuProps) {
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label="Training mark"
          title="Training mark"
          data-detection-mark-menu
          className={cn(
            "flex size-7 items-center justify-center rounded-md bg-black/50 text-white hover:bg-black/70",
            className,
          )}
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <LuMenu className="size-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        <DetectionMarkMenuItems
          eventIds={eventIds}
          MenuItem={DropdownMenuItem}
          onDone={onDone}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
