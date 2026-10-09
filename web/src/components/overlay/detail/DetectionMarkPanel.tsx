// LOCAL FORK: one-click "Real person / Not a person" marks for fine-tuning our
// own detector. Backend: frigate/api/detection_mark.py, table detection_mark.
import { useCallback, useState } from "react";
import useSWR from "swr";
import axios from "axios";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { refreshMarkViews } from "@/components/menu/DetectionMarkMenu";

type MarkSummary = { real: number; false: number; total: number };
type MarkResponse = { verdict: "real" | "false" | null; summary: MarkSummary };

type DetectionMarkPanelProps = {
  eventId: string;
  label: string;
};

export default function DetectionMarkPanel({
  eventId,
  label,
}: DetectionMarkPanelProps) {
  const { data, mutate } = useSWR<MarkResponse>(`events/${eventId}/mark`, {
    revalidateOnFocus: false,
  });
  const [busy, setBusy] = useState(false);

  const isPerson = label === "person";
  const realText = isPerson ? "Real person" : `Real ${label}`;
  const falseText = isPerson ? "Not a person" : `Not a ${label}`;

  const onMark = useCallback(
    async (verdict: "real" | "false") => {
      setBusy(true);
      try {
        // clicking the active verdict again removes the mark
        const resp =
          data?.verdict === verdict
            ? await axios.delete(`events/${eventId}/mark`)
            : await axios.post(`events/${eventId}/mark`, { verdict });
        const summary: MarkSummary = resp.data.summary;
        mutate(
          {
            verdict: data?.verdict === verdict ? null : verdict,
            summary,
          },
          false,
        );
        refreshMarkViews([eventId]); // tiles and menus elsewhere on the page
        if (resp.data.frame_saved === false) {
          toast.warning("Marked, but no clean snapshot frame was found", {
            position: "top-center",
          });
        }
      } catch (error) {
        toast.error("Could not save the mark", { position: "top-center" });
      } finally {
        setBusy(false);
      }
    },
    [data, eventId, mutate],
  );

  const summary = data?.summary;

  return (
    <div className="my-2 flex w-full flex-col gap-1.5">
      <div className="text-sm text-primary/40">Training mark</div>
      <div className="flex w-full flex-row gap-2">
        <Button
          className={cn(
            "flex-1",
            data?.verdict === "real" && "ring-2 ring-success",
          )}
          aria-label={realText}
          variant={data?.verdict === "real" ? "select" : "default"}
          disabled={busy || data === undefined}
          onClick={() => onMark("real")}
        >
          {realText}
        </Button>
        <Button
          className={cn(
            "flex-1",
            data?.verdict === "false" && "ring-2 ring-destructive",
          )}
          aria-label={falseText}
          variant={data?.verdict === "false" ? "destructive" : "default"}
          disabled={busy || data === undefined}
          onClick={() => onMark("false")}
        >
          {falseText}
        </Button>
      </div>
      {summary && (
        <div className="text-xs text-primary/40">
          Marked so far: {summary.real} real, {summary.false} not
          {data?.verdict ? " (click again to undo)" : ""}
        </div>
      )}
    </div>
  );
}
