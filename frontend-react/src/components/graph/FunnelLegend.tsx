import {
  FUNNEL_LEGEND_CAPTION,
  FUNNEL_STATES,
  LegendSwatch,
} from "../viz";

export function FunnelLegend() {
  return (
    <div className="funnel-legend" aria-label="Lifecycle funnel legend">
      <ul className="funnel-legend__states">
        {FUNNEL_STATES.map((state) => (
          <li key={state} className="funnel-legend__state">
            <LegendSwatch state={state} label={state} />
            <span className="funnel-legend__label">{state}</span>
          </li>
        ))}
      </ul>
      <p className="viz-legend__desc funnel-legend__caption">{FUNNEL_LEGEND_CAPTION}</p>
    </div>
  );
}
