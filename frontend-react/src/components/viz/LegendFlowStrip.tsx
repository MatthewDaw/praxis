import { stateNodeColors } from "../graph/graphLayout";
import { FLOW_REJECTED_STEP, FLOW_STRIP_STEPS } from "./legendConfig";

export function LegendFlowStrip() {
  return (
    <div className="legend-flow-strip" aria-hidden="true">
      <div className="legend-flow-strip__main">
        {FLOW_STRIP_STEPS.map((step, index) => {
          const colors = stateNodeColors(step.state);
          return (
            <div key={step.state} className="legend-flow-strip__segment">
              {index > 0 ? <span className="legend-flow-strip__arrow">→</span> : null}
              <span
                className="legend-flow-strip__node"
                style={{
                  background: colors.bg,
                  borderColor: colors.border,
                  color: colors.text,
                }}
              >
                {step.label}
              </span>
            </div>
          );
        })}
      </div>
      <div className="legend-flow-strip__branch">
        <span className="legend-flow-strip__arrow legend-flow-strip__arrow--down">↳</span>
        <span
          className="legend-flow-strip__node legend-flow-strip__node--rejected"
          style={{
            background: stateNodeColors(FLOW_REJECTED_STEP.state).bg,
            borderColor: stateNodeColors(FLOW_REJECTED_STEP.state).border,
            color: stateNodeColors(FLOW_REJECTED_STEP.state).text,
          }}
        >
          {FLOW_REJECTED_STEP.label}
        </span>
      </div>
    </div>
  );
}
