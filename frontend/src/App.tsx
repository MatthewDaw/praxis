/**
 * @file App.tsx
 * @usage Root dashboard shell for PRAXIS Human Gate UI. Renders sidebar nav + main CandidateList. Entry point for review workflow. Extend with routing/detail views in later days.
 * @example <App />
 * @author Monica Peters <monigarr@MoniGarr.com>
 * @created 2026-06-17
 */

import CandidateList from './components/CandidateList';
import CandidateDetail from './components/CandidateDetail';

function App() {
  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <header className="border-b bg-white px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-purple-600 rounded" />
          <h1 className="text-xl font-semibold tracking-tight">PRAXIS Dashboard</h1>
          <span className="text-xs px-2 py-0.5 bg-purple-100 text-purple-700 rounded">Human Gate</span>
        </div>
        <div className="text-sm text-gray-500">Monica Peters • Day 2 Shell</div>
      </header>

      <main className="max-w-5xl mx-auto">
        <CandidateList />
        <CandidateDetail />
      </main>

      <footer className="text-center text-xs text-gray-400 py-8">
        Provenance-linked • Keyboard accessible • Ready for backend integration (Days 6-7)
      </footer>
    </div>
  );
}

export default App;
