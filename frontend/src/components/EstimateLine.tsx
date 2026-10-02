import type { Estimate } from "../api/schemas";

interface Props {
  estimate: Estimate | null;
  minutes: number;
}

const plural = (count: number, word: string) => `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;

/** What generating this will cost, before anyone pays: sections, cache reuse, credits, budget. */
export function EstimateLine({ estimate, minutes }: Props) {
  const length = minutes < 1 ? "under a minute" : `about ${Math.round(minutes)} min`;
  if (!estimate) {
    return <p className="muted small">Roughly {length} of audio.</p>;
  }
  const { chunks, cached_chunks, billable_characters, estimated_credits, daily_budget_remaining } =
    estimate;
  const overBudget = billable_characters > daily_budget_remaining;
  const free = billable_characters === 0;

  return (
    <div className="estimate" aria-live="polite">
      <p className="small">
        <strong>{free ? "Free to generate" : `≈ ${plural(estimated_credits, "credit")}`}</strong>
        <span className="muted">
          {" "}
          · {plural(chunks, "section")}
          {cached_chunks > 0 && ` (${cached_chunks.toLocaleString()} already generated)`} · {length} of
          audio
        </span>
      </p>
      {free && cached_chunks > 0 && (
        <p className="muted small">Every section was generated recently, so it is reused at no cost.</p>
      )}
      <p className={overBudget ? "error small" : "muted small"}>
        {overBudget
          ? `This needs ${billable_characters.toLocaleString()} characters but only ${daily_budget_remaining.toLocaleString()} are left in today's budget.`
          : `${daily_budget_remaining.toLocaleString()} characters left in today's budget.`}
      </p>
    </div>
  );
}
