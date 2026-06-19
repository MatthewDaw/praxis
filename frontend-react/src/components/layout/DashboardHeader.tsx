import { EnvironmentBadge } from "../ui/EnvironmentBadge";

interface DashboardHeaderProps {
  apiUrl?: string;
  onRefresh: () => void;
}

export function DashboardHeader({ apiUrl, onRefresh }: DashboardHeaderProps) {
  return (
    <header className="dashboard-header">
      <div className="dashboard-header__brand">
        <p className="dashboard-header__eyebrow">PRAXIS</p>
        <h1 className="dashboard-header__title">Candidate Review Gate</h1>
        <p className="dashboard-header__subtitle">
          Review and promote AI-learned knowledge candidates from agent sessions.
        </p>
      </div>
      <div className="dashboard-header__meta">
        <EnvironmentBadge apiUrl={apiUrl} />
        <div className="dashboard-header__actions">
          <button type="button" className="btn primary" onClick={onRefresh}>
            Refresh data
          </button>
          <a
            className="contract-link"
            href="../docs/integration/candidate-api-v1.md"
            target="_blank"
            rel="noreferrer"
          >
            Contract: candidate-api-v1
          </a>
        </div>
      </div>
    </header>
  );
}
