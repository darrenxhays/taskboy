import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, timeAgo } from "../api";
import { EmptyState, ErrorNote, Panel, StatusBadge, TaskLink } from "../components/ui";
import { useLiveData } from "../stream";

function Stars({ rating, onSelect }: { rating: number; onSelect?: (value: number) => void }) {
  return (
    <span className="inline-flex gap-1">
      {[1, 2, 3, 4, 5].map((value) => (
        <button
          key={value}
          type="button"
          disabled={!onSelect}
          onClick={() => onSelect?.(value)}
          className={onSelect ? "text-[20px] transition-transform hover:scale-110" : "cursor-default text-[13px]"}
          style={{ color: value <= rating ? "var(--status-warning)" : "var(--text-muted)" }}
          aria-label={`${value} of 5`}
        >
          {value <= rating ? "★" : "☆"}
        </button>
      ))}
    </span>
  );
}

export function FeedbackPage({ email }: { email: string | null }) {
  const { taskId = "" } = useParams();
  const { data, error, reload } = useLiveData(() => api.task(taskId), [taskId]);
  const [rating, setRating] = useState(0);
  const [comment, setComment] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const mine = data?.feedback.find((row) => row.submitted_by === email);
  useEffect(() => {
    if (mine && !touched) {
      setRating(mine.rating);
      setComment(mine.comment ?? "");
    }
  }, [mine, touched]);

  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="loading task…" />;
  const task = data.task;

  const submit = async () => {
    setBusy(true);
    setNote(null);
    try {
      await api.submitFeedback(taskId, { rating, comment: comment.trim() });
      setNote(mine ? "feedback updated — thank you" : "feedback recorded — thank you");
      reload();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-[18px] font-bold tracking-wide">TASK FEEDBACK</h1>
        <TaskLink taskId={taskId} />
        <StatusBadge state={String(task.state)} pulse />
      </header>

      <Panel title="Request">
        <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed" style={{ color: "var(--text-primary)" }}>
          {String(task.request_text ?? "")}
        </p>
      </Panel>

      <Panel title={mine ? "Your feedback (resubmit to revise)" : "Your feedback"}>
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
              How did {data.bot_name || "the agent"} do?
            </span>
            <Stars
              rating={rating}
              onSelect={(value) => {
                setTouched(true);
                setRating(value);
              }}
            />
          </div>
          <textarea
            value={comment}
            onChange={(event) => {
              setTouched(true);
              setComment(event.target.value);
            }}
            rows={4}
            maxLength={4000}
            placeholder="what went well, what should improve…"
            className="w-full rounded-md border px-3 py-2 text-[13px] outline-none focus:border-cyan-500"
            style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }}
          />
          <div className="flex items-center gap-3">
            <button
              onClick={submit}
              disabled={busy || rating === 0}
              className="rounded-md px-4 py-1.5 text-[12px] font-bold text-white disabled:opacity-40"
              style={{ background: "var(--accent)" }}
            >
              SUBMIT FEEDBACK
            </button>
            {note && (
              <span className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
                {note}
              </span>
            )}
          </div>
        </div>
      </Panel>

      <Panel title="All feedback">
        {data.feedback.length === 0 ? (
          <EmptyState label="no feedback yet" />
        ) : (
          <div className="space-y-2.5">
            {data.feedback.map((row) => (
              <div key={row.id} className="text-[12px]">
                <div className="flex items-center gap-2">
                  <Stars rating={row.rating} />
                  <span style={{ color: "var(--text-secondary)" }}>{row.submitted_by}</span>
                  <span className="ml-auto" style={{ color: "var(--text-muted)" }}>
                    {timeAgo(row.updated_at)}
                  </span>
                </div>
                {row.comment && (
                  <p className="mt-1 whitespace-pre-wrap" style={{ color: "var(--text-primary)" }}>
                    {row.comment}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
