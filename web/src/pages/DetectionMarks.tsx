// LOCAL FORK: bulk review of person detections for training our own detector.
// Select tiles (drag a box, click, shift-click, ctrl-click, Ctrl+A), then mark the
// whole selection "Real person" or "Not a person" in one request.
// Drag behaviour follows /root/infra/docker-ui/frontend/src/ModelReview.jsx: a drag
// that starts on an unselected tile (or empty space) selects, one that starts on a
// selected tile deselects, and it never clears the rest of the selection.
// Backend: /api/detection_marks/candidates and /api/detection_marks/bulk.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import useSWR from "swr";
import { baseUrl } from "@/api/baseUrl";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { FrigateConfig } from "@/types/frigateConfig";
import {
  MarkSummary,
  MarkVerdict,
  markEvents,
} from "@/components/menu/DetectionMarkMenu";

type Candidate = {
  id: string;
  camera: string;
  start_time: number;
  score: number | null;
  verdict: "real" | "false" | null;
};

type CandidatesResponse = {
  items: Candidate[];
  total: number;
  summary: MarkSummary;
};

type Box = { x1: number; y1: number; x2: number; y2: number };

const PAGE = 300;
// after a mark, tiles keep their badge this long, then fade and leave the grid
const HIDE_MS = 3000;
const FADE_MS = 500;

function dayStart(date: string): number | undefined {
  return date ? new Date(`${date}T00:00:00`).getTime() / 1000 : undefined;
}

function dayEnd(date: string): number | undefined {
  return date ? new Date(`${date}T23:59:59`).getTime() / 1000 : undefined;
}

export default function DetectionMarks() {
  const { data: config } = useSWR<FrigateConfig>("config", {
    revalidateOnFocus: false,
  });

  useEffect(() => {
    document.title = "Mark detections - Frigate";
  }, []);

  // filters
  const [cameras, setCameras] = useState<string[]>([]); // empty = all
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [show, setShow] = useState<"all" | "unmarked" | "marked">("all");
  const [limit, setLimit] = useState(PAGE);
  // ids marked on this page that have left the grid ("Marked only" shows them)
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [fading, setFading] = useState<Set<string>>(new Set());

  const params = useMemo(
    () => ({
      cameras: cameras.length ? cameras.join(",") : "all",
      after: dayStart(fromDate),
      before: dayEnd(toDate),
      marked: show,
      // "All" lists marked items last; ask for the hidden ones on top so the
      // grid stays full after tiles leave it
      limit: show == "all" ? limit + hidden.size : limit,
    }),
    [cameras, fromDate, toDate, show, limit, hidden.size],
  );

  const { data, mutate, isLoading } = useSWR<CandidatesResponse>(
    ["detection_marks/candidates", params],
    { revalidateOnFocus: false, keepPreviousData: true },
  );
  // timers and the undo toast outlive renders; always use the current SWR key
  const mutateRef = useRef(mutate);
  mutateRef.current = mutate;
  const allItems = useMemo(() => data?.items ?? [], [data]);

  const items = useMemo(
    () =>
      show == "marked"
        ? allItems
        : allItems.filter((i) => !(i.verdict && hidden.has(i.id))),
    [allItems, hidden, show],
  );

  const cameraNames = useMemo(
    () =>
      Object.values(config?.cameras ?? {})
        .filter((c) => c.enabled_in_config)
        .map((c) => c.name)
        .sort(),
    [config],
  );

  // selection
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const anchorRef = useRef<number | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const tileRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const dragRef = useRef<{
    startX: number;
    startY: number;
    on: boolean;
    base: Set<string>;
    startId: string | null;
    moved: boolean;
  } | null>(null);
  const [box, setBox] = useState<Box | null>(null);

  // drop selected ids that are no longer listed
  useEffect(() => {
    setSelected((prev) => {
      const ids = new Set(items.map((i) => i.id));
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size == prev.size ? prev : next;
    });
  }, [items]);

  const selectAll = useCallback(
    () => setSelected(new Set(items.map((i) => i.id))),
    [items],
  );
  const clearSelection = useCallback(() => setSelected(new Set()), []);

  const onTileMouseDown = useCallback(
    (e: React.MouseEvent, id: string, index: number) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.shiftKey && anchorRef.current != null) {
        const [a, b] = [anchorRef.current, index].sort((x, y) => x - y);
        setSelected((prev) => {
          const next = new Set(prev);
          items.slice(a, b + 1).forEach((i) => next.add(i.id));
          return next;
        });
        return;
      }
      anchorRef.current = index;
      const on = !selected.has(id);
      dragRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        on,
        base: new Set(selected),
        startId: id,
        moved: false,
      };
    },
    [items, selected],
  );

  const onGridMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      dragRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        on: true,
        base: new Set(selected),
        startId: null,
        moved: false,
      };
    },
    [selected],
  );

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      if (
        !d.moved &&
        Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < 5
      )
        return;
      d.moved = true;
      const b: Box = {
        x1: Math.min(d.startX, e.clientX),
        y1: Math.min(d.startY, e.clientY),
        x2: Math.max(d.startX, e.clientX),
        y2: Math.max(d.startY, e.clientY),
      };
      setBox(b);
      const next = new Set(d.base);
      tileRefs.current.forEach((el, id) => {
        const r = el.getBoundingClientRect();
        const hit =
          r.left < b.x2 && r.right > b.x1 && r.top < b.y2 && r.bottom > b.y1;
        if (hit) {
          if (d.on) next.add(id);
          else next.delete(id);
        }
      });
      setSelected(next);
    };
    const onUp = () => {
      const d = dragRef.current;
      dragRef.current = null;
      setBox(null);
      if (d && !d.moved && d.startId) {
        // a click without a drag toggles that tile (Docker UI behaviour)
        const id = d.startId;
        setSelected((prev) => {
          const next = new Set(prev);
          if (d.on) next.add(id);
          else next.delete(id);
          return next;
        });
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  // marking
  const [busy, setBusy] = useState(false);
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  useEffect(() => {
    const timers = timersRef.current;
    return () => timers.forEach((t) => clearTimeout(t));
  }, []);

  // keep the first visible tile in place when tiles above it leave the grid
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollAnchor = useRef<{ id: string; top: number } | null>(null);
  const captureAnchor = useCallback((leaving: Set<string>) => {
    const box = scrollRef.current?.getBoundingClientRect();
    if (!box) return;
    for (const [id, el] of tileRefs.current) {
      if (leaving.has(id)) continue;
      const r = el.getBoundingClientRect();
      if (r.bottom > box.top) {
        if (!scrollAnchor.current || r.top < scrollAnchor.current.top)
          scrollAnchor.current = { id, top: r.top };
      }
    }
  }, []);
  useLayoutEffect(() => {
    const a = scrollAnchor.current;
    scrollAnchor.current = null;
    const el = a && tileRefs.current.get(a.id);
    if (a && el && scrollRef.current)
      scrollRef.current.scrollTop += el.getBoundingClientRect().top - a.top;
  }, [items]);

  const setVerdicts = useCallback(
    (ids: Set<string>, verdict: "real" | "false" | null, summary?: MarkSummary) =>
      mutateRef.current(
        (cur) =>
          cur && {
            ...cur,
            summary: summary ?? cur.summary,
            items: cur.items.map((i) => (ids.has(i.id) ? { ...i, verdict } : i)),
          },
        false,
      ),
    [],
  );

  const later = useCallback((fn: () => void, ms: number) => {
    const t = setTimeout(() => {
      timersRef.current.delete(t);
      fn();
    }, ms);
    timersRef.current.add(t);
    return t;
  }, []);

  const mark = useCallback(
    async (verdict: MarkVerdict) => {
      const ids = items.filter((i) => selected.has(i.id)).map((i) => i.id);
      if (!ids.length) return;
      setBusy(true);
      const leave = verdict != "clear" && show != "marked";
      const r = await markEvents(ids, verdict, { quiet: leave });
      setBusy(false);
      if (!r) return;
      const skipped = new Set(r.skipped.concat(r.missing));
      const done = new Set(
        verdict == "clear" ? ids : ids.filter((id) => !skipped.has(id)),
      );
      setVerdicts(done, verdict == "clear" ? null : verdict, r.summary);
      setSelected(new Set());
      if (!leave || done.size == 0) return;

      const t1 = later(
        () => setFading((p) => new Set([...p, ...done])),
        HIDE_MS,
      );
      const t2 = later(() => {
        captureAnchor(done);
        setHidden((p) => new Set([...p, ...done]));
        setFading((p) => new Set([...p].filter((id) => !done.has(id))));
        mutateRef.current(); // refill the grid with the next unmarked candidates
      }, HIDE_MS + FADE_MS);

      const what = verdict == "real" ? "real person" : "not a person";
      toast.success(`Marked ${done.size} ${what}`, {
        position: "top-center",
        duration: HIDE_MS + FADE_MS,
        action: {
          label: "Undo",
          onClick: async () => {
            [t1, t2].forEach((t) => {
              clearTimeout(t);
              timersRef.current.delete(t);
            });
            setFading((p) => new Set([...p].filter((id) => !done.has(id))));
            setHidden((p) => new Set([...p].filter((id) => !done.has(id))));
            const u = await markEvents([...done], "clear", { quiet: true });
            if (!u) return;
            setVerdicts(done, null, u.summary);
            mutateRef.current();
            toast.success(`Undone: cleared ${u.changed}`, {
              position: "top-center",
            });
          },
        },
      });
    },
    [items, selected, show, setVerdicts, later, captureAnchor],
  );

  // keyboard: Ctrl+A select all, Esc clear, R real, N not a person, C clear mark
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag == "INPUT" || tag == "TEXTAREA" || tag == "SELECT") return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() == "a") {
        e.preventDefault();
        selectAll();
      } else if (e.key == "Escape") {
        clearSelection();
      } else if (!e.ctrlKey && !e.metaKey && !e.altKey && !busy) {
        const k = e.key.toLowerCase();
        if (k == "r") mark("real");
        else if (k == "n") mark("false");
        else if (k == "c") mark("clear");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectAll, clearSelection, mark, busy]);

  const summary = data?.summary;
  const unmarkedShown = items.filter((i) => i.verdict == null).length;

  return (
    <div
      className="flex size-full flex-col overflow-hidden"
      data-testid="detection-marks-page"
    >
      {/* the page had no Toaster, so mark toasts (and Undo) never showed */}
      <Toaster position="top-center" closeButton={true} />
      {/* filters */}
      <div className="flex flex-wrap items-center gap-2 border-b border-secondary p-2 text-sm">
        <div className="font-medium">Mark detections</div>
        <div className="flex flex-wrap gap-1" data-testid="camera-filter">
          <Button
            size="sm"
            className="h-7 px-2"
            variant={cameras.length == 0 ? "select" : "default"}
            onClick={() => setCameras([])}
          >
            All cameras
          </Button>
          {cameraNames.map((name) => (
            <Button
              key={name}
              size="sm"
              className="h-7 px-2"
              variant={cameras.includes(name) ? "select" : "default"}
              onClick={() =>
                setCameras((prev) =>
                  prev.includes(name)
                    ? prev.filter((c) => c != name)
                    : [...prev, name],
                )
              }
            >
              {name}
            </Button>
          ))}
        </div>
        <label className="flex items-center gap-1">
          From
          <input
            type="date"
            className="rounded bg-secondary px-1"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-1">
          To
          <input
            type="date"
            className="rounded bg-secondary px-1"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
          />
        </label>
        <select
          className="rounded bg-secondary px-1 py-0.5"
          value={show}
          onChange={(e) => setShow(e.target.value as typeof show)}
        >
          <option value="all">All (unmarked first)</option>
          <option value="unmarked">Unmarked only</option>
          <option value="marked">Marked only</option>
        </select>
      </div>

      {/* action bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-secondary p-2 text-sm">
        <span data-testid="selected-count" className="min-w-24">
          {selected.size} selected
        </span>
        <Button
          size="sm"
          className="bg-success text-white hover:bg-success/80"
          disabled={busy || selected.size == 0}
          onClick={() => mark("real")}
        >
          Real person (R)
        </Button>
        <Button
          size="sm"
          variant="destructive"
          disabled={busy || selected.size == 0}
          onClick={() => mark("false")}
        >
          Not a person (N)
        </Button>
        <Button
          size="sm"
          disabled={busy || selected.size == 0}
          onClick={() => mark("clear")}
        >
          Clear mark (C)
        </Button>
        <Button size="sm" variant="outline" onClick={selectAll}>
          Select all
        </Button>
        <Button size="sm" variant="outline" onClick={clearSelection}>
          Select none
        </Button>
        <span
          className="ml-auto text-xs text-muted-foreground"
          data-testid="mark-counts"
        >
          {summary
            ? `Marked: ${summary.real} real, ${summary.false} not a person`
            : ""}
          {data
            ? ` · showing ${items.length} of ${data.total}, ${unmarkedShown} unmarked`
            : ""}
        </span>
      </div>

      <div className="px-2 pt-1 text-xs text-muted-foreground">
        Drag a box to select, click to toggle, shift-click for a range, Ctrl+A
        for all, Esc to clear.
      </div>

      {/* grid */}
      <div
        ref={scrollRef}
        className="relative flex-1 select-none overflow-y-auto p-2 [overflow-anchor:none]"
        onMouseDown={onGridMouseDown}
      >
        <div
          ref={gridRef}
          className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-8 2xl:grid-cols-10"
          data-testid="mark-grid"
        >
          {items.map((item, index) => {
            const isSel = selected.has(item.id);
            return (
              <div
                key={item.id}
                data-mark-tile={item.id}
                data-selected={isSel ? "1" : "0"}
                data-verdict={item.verdict ?? ""}
                data-fading={fading.has(item.id) ? "1" : "0"}
                ref={(el) => {
                  if (el) tileRefs.current.set(item.id, el);
                  else tileRefs.current.delete(item.id);
                }}
                onMouseDown={(e) => onTileMouseDown(e, item.id, index)}
                title={`${item.camera} ${new Date(item.start_time * 1000).toLocaleString()}${
                  item.score ? ` score ${Math.round(item.score * 100)}%` : ""
                }`}
                className={cn(
                  "relative aspect-square cursor-pointer overflow-hidden rounded-md bg-secondary outline outline-2 -outline-offset-2",
                  isSel ? "outline-selected" : "outline-transparent",
                  "transition-opacity duration-500",
                  fading.has(item.id) && "pointer-events-none opacity-0",
                )}
              >
                <img
                  src={`${baseUrl}api/events/${item.id}/thumbnail.webp`}
                  loading="lazy"
                  draggable={false}
                  className={cn(
                    "size-full object-cover",
                    item.verdict && !isSel && "opacity-60",
                  )}
                  alt=""
                />
                {isSel && (
                  <div className="pointer-events-none absolute inset-0 bg-selected/25" />
                )}
                {item.verdict && (
                  <div
                    className={cn(
                      "absolute left-1 top-1 rounded px-1 text-[10px] font-semibold text-white",
                      item.verdict == "real" ? "bg-success" : "bg-destructive",
                    )}
                  >
                    {item.verdict == "real" ? "REAL" : "NOT"}
                  </div>
                )}
                <div className="pointer-events-none absolute inset-x-0 bottom-0 truncate bg-black/50 px-1 text-[10px] text-white">
                  {item.camera}{" "}
                  {new Date(item.start_time * 1000).toLocaleString([], {
                    month: "numeric",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </div>
              </div>
            );
          })}
        </div>
        {!isLoading && items.length == 0 && (
          <div className="p-8 text-center text-muted-foreground">
            No person detections match these filters.
          </div>
        )}
        {data && allItems.length < data.total && (
          <div className="flex justify-center p-4">
            <Button
              size="sm"
              onMouseDown={(e) => e.stopPropagation()}
              onClick={() => setLimit((l) => l + PAGE)}
            >
              Load {Math.min(PAGE, data.total - allItems.length)} more
            </Button>
          </div>
        )}
      </div>

      {box && (
        <div
          className="pointer-events-none fixed z-50 border border-selected bg-selected/20"
          style={{
            left: box.x1,
            top: box.y1,
            width: box.x2 - box.x1,
            height: box.y2 - box.y1,
          }}
        />
      )}
    </div>
  );
}
