/**
 * @file ConfidenceScore.tsx
 * @usage Reusable confidence visualization component showing frequency/recency/breadth scores with tooltips. Used in CandidateList and detail views. Matches PRAXIS dashboard rules for credibility indicators.
 * @example <ConfidenceScore score={candidate.confidence} />
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

import type { ConfidenceScore as Score } from '../types/candidate';

interface Props {
  score: Score;
}

export default function ConfidenceScore({ score }: Props) {
  const avg = ((score.frequency + score.recency + score.breadth) / 3) * 100;
  const tooltip = score.rationale || `Freq: ${(score.frequency * 100).toFixed(0)}% | Rec: ${(score.recency * 100).toFixed(0)}% | Br: ${(score.breadth * 100).toFixed(0)}%`;

  return (
    <div className="inline-flex items-center gap-2 text-sm" title={tooltip}>
      <div className="w-16 h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className="h-full bg-purple-600 transition-all"
          style={{ width: `${avg}%` }}
        />
      </div>
      <span className="font-mono text-xs text-gray-500">{avg.toFixed(0)}%</span>
    </div>
  );
}
