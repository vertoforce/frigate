// LOCAL FORK: per-event "Real person / Not a person / Clear mark" menu.
// Backend: POST /api/detection_marks/bulk and /lookup (frigate/api/detection_mark.py).
import { ElementType, useCallback, useMemo } from "react";
import axios from "axios";
import { toast } from "sonner";
import { LuCheck, LuMenu } from "react-icons/lu";
import useSWR, { mutate } from "swr";
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

export type MarkVerdicts = Record<string, "real" | "false">;

const LOOKUP = "detection_marks/lookup";

// Re-fetch every view that shows marks: menus/tiles (lookup) and the details pane.
export function refreshMarkViews(eventIds: string[]) {
  mutate((key) => Array.isArray(key) && key[0] === LOOKUP);
  eventIds.forEach((id) => mutate(`events/${id}/mark`));
}

// Current verdicts for a list of events, one POST for the whole list.
export function useMarkVerdicts(eventIds: string[], enabled = true) {
  const joined = useMemo(() => [...eventIds].sort().join(","), [eventIds]);
  const { data } = useSWR<MarkVerdicts>(
    enabled && joined ? [LOOKUP, joined] : null,
    ([url, ids]: [string, string]) =>
      axios
        .post(url, { event_ids: ids.split(",") })
        .then((r) => r.data.verdicts as MarkVerdicts),
    { revalidateOnFocus: false },
  );
  return data;
}

// "real" / "false" when every event has that verdict, "mixed" when some are
// marked differently or not at all, null when none are marked.
export function combinedVerdict(
  eventIds: string[],
  verdicts: MarkVerdicts | undefined,
): "real" | "false" | "mixed" | null {
  if (!verdicts || eventIds.length == 0) return null;
  const vals = eventIds.map((id) => verdicts[id]);
  if (vals.every((v) => v == undefined)) return null;
  if (vals.every((v) => v == vals[0])) return vals[0] ?? null;
  return "mixed";
}

export async function markEvents(
  eventIds: string[],
  verdict: MarkVerdict,
  opts?: { quiet?: boolean }, // quiet: caller shows its own success toast
): Promise<BulkMarkResult | undefined> {
  if (eventIds.length == 0) return undefined;
  try {
    const resp = await axios.post("detection_marks/bulk", {
      event_ids: eventIds,
      verdict,
      person_only: true,
    });
    const r: BulkMarkResult = resp.data;
    refreshMarkViews(eventIds);
    if (opts?.quiet) return r;
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

const ITEMS: { verdict: MarkVerdict; text: string }[] = [
  { verdict: "real", text: "Real person" },
  { verdict: "false", text: "Not a person" },
  { verdict: "clear", text: "Clear mark" },
];

// Rendered inside menu content, so it mounts (and loads the verdicts) only when
// the menu opens.
export function DetectionMarkMenuItems({
  eventIds,
  MenuItem,
  onDone,
}: DetectionMarkMenuItemsProps) {
  const verdicts = useMarkVerdicts(eventIds);
  const current = combinedVerdict(eventIds, verdicts);
  const run = useCallback(
    async (verdict: MarkVerdict) => {
      // NB: not `onDone?.(await markEvents(...))`: an optional call skips its
      // arguments when onDone is undefined, so nothing would be sent.
      const r = await markEvents(eventIds, verdict);
      onDone?.(r);
    },
    [eventIds, onDone],
  );
  return (
    <>
      {ITEMS.map(({ verdict, text }) => {
        const checked = verdict != "clear" && current == verdict;
        return (
          <MenuItem
            key={verdict}
            className="cursor-pointer"
            data-mark-verdict={verdict}
            data-checked={checked ? "1" : "0"}
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              run(verdict);
            }}
          >
            <span className="mr-2 flex w-4 justify-center">
              {checked && <LuCheck className="size-4" />}
            </span>
            <span>{text}</span>
            {verdict == "clear" && current == "mixed" && (
              <span className="ml-2 text-xs text-muted-foreground">
                (marks differ)
              </span>
            )}
          </MenuItem>
        );
      })}
    </>
  );
}

type DetectionMarkMenuProps = {
  eventIds: string[];
  className?: string;
  // verdict of this tile, from the page's batched useMarkVerdicts
  verdict?: "real" | "false" | "mixed" | null;
  onDone?: (r: BulkMarkResult | undefined) => void;
};

// Small three-line button for review tiles.
export default function DetectionMarkMenu({
  eventIds,
  className,
  verdict,
  onDone,
}: DetectionMarkMenuProps) {
  const badge =
    verdict == "real"
      ? "REAL"
      : verdict == "false"
        ? "NOT"
        : verdict == "mixed"
          ? "MIXED"
          : null;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label="Training mark"
          title="Training mark"
          data-detection-mark-menu
          data-verdict={verdict ?? ""}
          className={cn(
            "flex h-7 min-w-7 items-center justify-center gap-1 rounded-md px-1.5 text-white",
            verdict == "real"
              ? "bg-success/90 hover:bg-success"
              : verdict == "false"
                ? "bg-destructive/90 hover:bg-destructive"
                : "bg-black/50 hover:bg-black/70",
            className,
          )}
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {badge && <span className="text-xs font-semibold">{badge}</span>}
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
