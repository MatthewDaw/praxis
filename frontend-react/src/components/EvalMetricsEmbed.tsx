import { useEffect, useState } from "react";
import type { DataProvider } from "../api/dataProvider";
import type { EvalMetrics } from "../types/candidate";

interface EvalMetricsEmbedProps {
  provider: DataProvider;
}

export function EvalMetricsEmbed({ provider }: EvalMetricsEmbedProps) {
  const [open, setOpen] = useState(false);
  const [metrics, setMetrics] = useState<EvalMetrics | null>(null);

  useEffect(() => {
    if (!open || metrics) {
      return;
    }
    void provider.getEvalMetrics().then(setMetrics);
  }, [open, metrics, provider]);

  const series = metrics?.correctionRate ?? [1.0, 0.72, 0.48, 0.35];
  const labels =
    metrics?.sessions && metrics.sessions.length === series.length
      ? metrics.sessions
      : series.map((_, index) => `run_${index}`);

  const max = Math.max(...series, 1);
  const reduction =
    metrics?.correctionsBefore != null &&
    metrics.correctionsAfter != null &&
    metrics.correctionsBefore > 0
      ? Math.round(
          (1 - metrics.correctionsAfter / metrics.correctionsBefore) * 100,
        )
      : null;

  return (
    <section className="eval-panel">
      <button
        type="button"
        className="eval-toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        Eval metrics — compounding curve
      </button>
      {open ? (
        <div className="eval-body">
          <p className="muted">
            {metrics?.source === "placeholder"
              ? "Placeholder curve — set VITE_PRAXIS_EVAL_METRICS_URL to Dominic's eval metrics endpoint when available."
              : `Loaded from ${metrics?.source}`}
          </p>
          <div className="chart" role="img" aria-label="Correction rate compounding curve">
            {series.map((value, index) => (
              <div key={labels[index]} className="chart-bar-wrap">
                <div
                  className="chart-bar"
                  style={{ height: `${(value / max) * 100}%` }}
                  title={`${labels[index]}: ${value}`}
                />
                <span>{labels[index]}</span>
              </div>
            ))}
          </div>
          {metrics?.correctionsBefore != null && metrics.correctionsAfter != null ? (
            <div className="metric-row">
              <div>
                <span className="metric-label">Corrections (cold)</span>
                <strong>{metrics.correctionsBefore}</strong>
              </div>
              <div>
                <span className="metric-label">Corrections (with PRAXIS)</span>
                <strong>{metrics.correctionsAfter}</strong>
              </div>
              <div>
                <span className="metric-label">Reduction</span>
                <strong>{reduction != null ? `${reduction}%` : "—"}</strong>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
