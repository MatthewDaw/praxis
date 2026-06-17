/**
 * @file candidateStore.ts
 * @usage Global Zustand store for dashboard candidate state management. Provides list, selection, and human-gate actions (promote, reject, resolve). Reusable in any React component via useCandidateStore hook. Persists in-memory for MVP; extend with localStorage later.
 * @example import { useCandidateStore } from './store/candidateStore'; const { candidates, promoteCandidate } = useCandidateStore();
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

import { create } from 'zustand';
import type { Candidate, GateState } from '../types/candidate';

interface CandidateState {
  candidates: Candidate[];
  selectedId: string | null;
  setCandidates: (candidates: Candidate[]) => void;
  selectCandidate: (id: string) => void;
  deselectCandidate: () => void;
  promoteCandidate: (id: string) => void;
  rejectCandidate: (id: string) => void;
  resolveContradiction: (id: string, resolution: string) => void;
}

export const useCandidateStore = create<CandidateState>((set) => ({
  candidates: [],
  selectedId: null,
  setCandidates: (candidates) => set({ candidates }),
  selectCandidate: (id) => set({ selectedId: id }),
  deselectCandidate: () => set({ selectedId: null }),
  promoteCandidate: (id) =>
    set((state) => ({
      candidates: state.candidates.map((c) =>
        c.id === id ? { ...c, state: 'active' as GateState } : c
      ),
    })),
  rejectCandidate: (id) =>
    set((state) => ({
      candidates: state.candidates.filter((c) => c.id !== id),
    })),
  resolveContradiction: (id, resolution) =>
    set((state) => ({
      candidates: state.candidates.map((c) =>
        c.id === id
          ? {
              ...c,
              contradictions: c.contradictions
                ? [...c.contradictions, resolution]
                : [resolution],
            }
          : c
      ),
    })),
}));
