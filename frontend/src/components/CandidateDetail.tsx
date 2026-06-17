/**
 * @file CandidateDetail.tsx
 * @usage Modal detail view for selected knowledge candidate. Shows full content, expanded confidence breakdown with per-metric bars, provenance audit trail with source log link, state badge with visual transition, and human-gate actions. Triggered via store.selectedId. Keyboard accessible (ESC closes). Reused ConfidenceScore for consistency.
 * @example <CandidateDetail />
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

import { useEffect } from 'react';
import { useCandidateStore } from '../store/candidateStore';
import type { Candidate } from '../types/candidate';
import ConfidenceScore from './ConfidenceScore';

export default function CandidateDetail() {
  const { candidates, selectedId, deselectCandidate, promoteCandidate, rejectCandidate, resolveContradiction } = useCandidateStore();

  const candidate = candidates.find((c) => c.id === selectedId) as Candidate | undefined;

  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        deselectCandidate();
      }
    };
    if (selectedId) {
      document.addEventListener('keydown', handleEscape);
    }
    return () => document.removeEventListener('keydown', handleEscape);
  }, [selectedId, deselectCandidate]);

  if (!selectedId || !candidate) return null;

  const handlePromote = () => {
    promoteCandidate(candidate.id);
    deselectCandidate();
  };

  const handleReject = () => {
    rejectCandidate(candidate.id);
    deselectCandidate();
  };

  const handleResolve = () => {
    // Stub for Day 5 contradiction resolution UI; logs action with provenance for audit
    const resolution = `Resolved via detail view at ${new Date().toISOString()}`;
    resolveContradiction(candidate.id, resolution);
    // In full impl: open side-by-side comparison modal
    alert('Contradiction resolution stubbed (Day 5). Action recorded with provenance.');
  };

  const stateColor = candidate.state === 'active'
    ? 'bg-green-100 text-green-700 border-green-200'
    : candidate.state === 'suggested'
      ? 'bg-blue-100 text-blue-700 border-blue-200'
      : 'bg-yellow-100 text-yellow-700 border-yellow-200';

  const avg = ((candidate.confidence.frequency + candidate.confidence.recency + candidate.confidence.breadth) / 3) * 100;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center z-50"
      onClick={deselectCandidate}
      role="dialog"
      aria-modal="true"
      aria-labelledby="detail-title"
    >
      <div
        className="bg-white rounded-xl shadow-xl max-w-2xl w-full mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 pt-6 pb-4 border-b flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h2 id="detail-title" className="text-2xl font-semibold">{candidate.title}</h2>
              <span
                className={`px-3 py-0.5 text-sm rounded-full border capitalize transition-colors ${stateColor}`}
              >
                {candidate.state}
              </span>
            </div>
            <div className="text-xs text-gray-500 font-mono">
              Created {new Date(candidate.createdAt).toLocaleDateString()}
            </div>
          </div>
          <button
            onClick={deselectCandidate}
            className="text-gray-400 hover:text-gray-600 text-2xl leading-none"
            aria-label="Close detail view"
          >
            ×
          </button>
        </div>

        <div className="px-6 py-5 space-y-6 text-sm">
          <div>
            <div className="font-medium text-gray-700 mb-1">Lesson / Pattern</div>
            <p className="text-gray-800 leading-relaxed whitespace-pre-wrap">{candidate.content}</p>
          </div>

          <div>
            <div className="font-medium text-gray-700 mb-2">Confidence Breakdown</div>
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <div className="w-20 text-xs text-gray-500">Frequency</div>
                <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
                  <div className="h-full bg-purple-600 transition-all" style={{ width: `${candidate.confidence.frequency * 100}%` }} />
                </div>
                <span className="font-mono text-xs w-10 text-right">{(candidate.confidence.frequency * 100).toFixed(0)}%</span>
              </div>
              <div className="flex items-center gap-3">
                <div className="w-20 text-xs text-gray-500">Recency</div>
                <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
                  <div className="h-full bg-purple-600 transition-all" style={{ width: `${candidate.confidence.recency * 100}%` }} />
                </div>
                <span className="font-mono text-xs w-10 text-right">{(candidate.confidence.recency * 100).toFixed(0)}%</span>
              </div>
              <div className="flex items-center gap-3">
                <div className="w-20 text-xs text-gray-500">Breadth</div>
                <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
                  <div className="h-full bg-purple-600 transition-all" style={{ width: `${candidate.confidence.breadth * 100}%` }} />
                </div>
                <span className="font-mono text-xs w-10 text-right">{(candidate.confidence.breadth * 100).toFixed(0)}%</span>
              </div>
            </div>
            <div className="mt-3 flex items-center gap-2 text-sm">
              <span className="text-gray-500">Overall:</span>
              <ConfidenceScore score={candidate.confidence} />
              <span className="font-mono text-xs text-gray-500">{avg.toFixed(0)}%</span>
            </div>
            {candidate.confidence.rationale && (
              <div className="mt-1 text-xs text-gray-500 italic">{candidate.confidence.rationale}</div>
            )}
          </div>

          <div>
            <div className="font-medium text-gray-700 mb-1">Provenance &amp; Audit Trail</div>
            <div className="font-mono text-xs bg-gray-50 border rounded px-3 py-2 text-gray-600 break-all" title="Source log line for full traceability">
              {candidate.provenance.sourceLogPath}:{candidate.provenance.lineOffset}
            </div>
            <div className="text-[10px] text-gray-400 mt-1">Clicking a candidate in list or actions here links back to originating JSONL for interview storytelling.</div>
          </div>

          {candidate.contradictions && candidate.contradictions.length > 0 && (
            <div>
              <div className="font-medium text-gray-700 mb-1">Recorded Resolutions</div>
              <ul className="text-xs text-gray-600 list-disc pl-4">
                {candidate.contradictions.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t bg-gray-50 flex gap-3 justify-end">
          <button
            onClick={handleResolve}
            className="text-xs px-4 py-1.5 border rounded hover:bg-white"
            aria-label="Resolve contradictions for this candidate"
          >
            Resolve Contradiction
          </button>
          <button
            onClick={handleReject}
            className="text-xs px-4 py-1.5 border rounded hover:bg-red-50"
            aria-label="Reject this candidate"
          >
            Reject
          </button>
          <button
            onClick={handlePromote}
            className="text-xs px-4 py-1.5 bg-purple-600 text-white rounded hover:bg-purple-700"
            aria-label="Promote candidate to active state"
          >
            Promote to Active
          </button>
        </div>
      </div>
    </div>
  );
}
