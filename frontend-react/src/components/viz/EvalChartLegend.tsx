import type { ReactNode } from "react";
import { EVAL_AXIS_LABELS, EVAL_METRIC_LEGEND, LegendItem } from "./index";

interface EvalChartLegendProps {
  maxValue: number;
  reductionPercent: number | null;
  children: ReactNode;
}

function formatTick(value: number): string {
  return value.toFixed(2);
}

export function EvalChartLegend({
  maxValue,
  reductionPercent,
  children,
}: EvalChartLegendProps) {
  const mid = maxValue / 2;

  return (
    <div className="eval-chart-legend" aria-label="Eval compounding curve legend">
      <ul className="viz-legend__list eval-chart-legend__series">
        <LegendItem
          marker={<span className="eval-legend__marker eval-legend__marker--series" />}
          label={EVAL_AXIS_LABELS.series}
          description={EVAL_AXIS_LABELS.seriesDescription}
        />
      </ul>

      <div className="chart-with-axes">
        <div className="chart-with-axes__y">
          <span className="chart-axis chart-axis--y">{EVAL_AXIS_LABELS.y}</span>
          <span className="chart-axis chart-axis--y-hint">{EVAL_AXIS_LABELS.yHint}</span>
          <div className="chart-y-ticks" aria-hidden="true">
            <span>{formatTick(maxValue)}</span>
            <span>{formatTick(mid)}</span>
            <span>0.00</span>
          </div>
        </div>
        <div className="chart-with-axes__plot">{children}</div>
        <span className="chart-axis chart-axis--x">{EVAL_AXIS_LABELS.x}</span>
      </div>

      <ul className="viz-legend__list eval-legend">
        {EVAL_METRIC_LEGEND.map((entry) => {
          const markerClass =
            entry.id === "reduction" && reductionPercent != null && reductionPercent >= 50
              ? "eval-legend__marker eval-legend__marker--reduction eval-legend__marker--success"
              : `eval-legend__marker ${entry.markerClass}`;
          return (
            <LegendItem
              key={entry.id}
              marker={<span className={markerClass} />}
              label={entry.label}
              description={entry.description}
            />
          );
        })}
      </ul>
    </div>
  );
}
