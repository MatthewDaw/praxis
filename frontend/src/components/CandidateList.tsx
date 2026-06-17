/**
 * @file CandidateList.tsx
 * @usage Main list view for human review gate. Displays candidates filtered by state, with confidence viz, provenance links, and action buttons. Supports keyboard nav (tab/enter). Reusable for dashboard shell and modals.
 * @example <CandidateList onSelect={setSelected} />
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

import { useState, useEffect } from 'react';
import { useCandidateStore } from '../store/candidateStore';
import type { Candidate, GateState } from '../types/candidate';
import ConfidenceScore from './ConfidenceScore';

const MOCK_CANDIDATES: Candidate[] = [
  {
    id: 'c1',
    title: 'Use semantic HTML for buttons',
    content: 'Always prefer <button> over div[role=button] for accessibility and keyboard support.',
    state: 'proposed',
    confidence: { frequency: 0.85, recency: 0.92, breadth: 0.7, rationale: 'High freq in UI tasks, recent sessions' },
    provenance: { sourceLogPath: 'logs/session-042.jsonl', lineOffset: 127 },
    createdAt: '2026-06-16T10:00:00Z',
  },
  {
    id: 'c2',
    title: 'Handle TS exhaustive switch with never',
    content: 'Default case: const _exhaustive: never = x; ensures compile-time safety on new union members.',
    state: 'suggested',
    confidence: { frequency: 0.6, recency: 0.8, breadth: 0.9 },
    provenance: { sourceLogPath: 'logs/session-039.jsonl', lineOffset: 45 },
    createdAt: '2026-06-15T14:30:00Z',
  },
];

export default function CandidateList() {
  const { candidates, setCandidates, selectCandidate, promoteCandidate, rejectCandidate } = useCandidateStore();
  const [filter, setFilter] = useState<GateState | 'all'>('all');

  useEffect(() => {
    if (candidates.length === 0) setCandidates(MOCK_CANDIDATES);
  }, [candidates.length, setCandidates]);

  const filtered = candidates.filter((c) => filter === 'all' || c.state === filter);

  return (
    <div className="p-6">
      <div className="flex justify-between mb-4">
        <h2 className="text-2xl font-semibold">Knowledge Candidates</h2>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as any)}
          className="border rounded px-3 py-1"
        >
          <option value="all">All States</option>
          <option value="proposed">Proposed</option>
          <option value="suggested">Suggested</option>
          <option value="active">Active</option>
        </select>
      </div>

      <div className="space-y-3">
        {filtered.length === 0 && <p className="text-gray-500">No candidates match filter.</p>}
        {filtered.map((candidate) => (
          <div
            key={candidate.id}
            className="border rounded-lg p-4 hover:shadow cursor-pointer flex flex-col gap-2"
            onClick={() => selectCandidate(candidate.id)}
            onKeyDown={(e) => e.key === 'Enter' && selectCandidate(candidate.id)}
            tabIndex={0}
          >
            <div className="flex justify-between items-start">
              <div>
                <h3 className="font-medium text-lg">{candidate.title}</h3>
                <p className="text-sm text-gray-600 line-clamp-2">{candidate.content}</p>
              </div>
              <span className={`px-2 py-0.5 text-xs rounded-full capitalize ${candidate.state === 'active' ? 'bg-green-100 text-green-700' : candidate.state === 'suggested' ? 'bg-blue-100 text-blue-700' : 'bg-yellow-100 text-yellow-700'}`}>
                {candidate.state}
              </span>
            </div>

            <div className="flex items-center justify-between text-sm">
              <ConfidenceScore score={candidate.confidence} />
              <div className="text-xs text-gray-500 font-mono truncate max-w-[200px]" title={candidate.provenance.sourceLogPath}>
                {candidate.provenance.sourceLogPath}:{candidate.provenance.lineOffset}
              </div>
            </div>

            <div className="flex gap-2 mt-2">
              <button
                onClick={(e) => { e.stopPropagation(); promoteCandidate(candidate.id); }}
                className="text-xs px-3 py-1 bg-purple-600 text-white rounded hover:bg-purple-700"
              >
                Promote
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); rejectCandidate(candidate.id); }}
                className="text-xs px-3 py-1 border rounded hover:bg-red-50"
              >
                Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
