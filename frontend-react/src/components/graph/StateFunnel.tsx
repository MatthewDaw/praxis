import { FunnelLegend } from "./FunnelLegend";
import { FUNNEL_STATES } from "../viz";
import type { Candidate } from "../../types/candidate";

interface StateFunnelProps {
  candidates: Candidate[];
}

export function StateFunnel({ candidates }: StateFunnelProps) {
  const counts = FUNNEL_STATES.map((state) => ({
    state,
    count: candidates.filter((c) => c.state === state).length,
  }));
  const max = Math.max(...counts.map((c) => c.count), 1);

  return (
    <section className="state-funnel" aria-label="Lifecycle state distribution">
      <p className="state-funnel__label">Lifecycle funnel</p>
      <FunnelLegend />
      <div className="state-funnel__bars">
        {counts.map(({ state, count }) => (
          <div key={state} className="state-funnel__item">
            <div
              className={`state-funnel__bar state-funnel__bar--${state}`}
              style={{ height: `${(count / max) * 100}%` }}
              title={`${state}: ${count}`}
            />
            <span className="state-funnel__count">{count}</span>
            <span className="state-funnel__state">{state}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
