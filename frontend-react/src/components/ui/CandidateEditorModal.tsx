import { useEffect, useId, useState, type FormEvent } from "react";
import type { Candidate, CandidateWriteInput } from "../../types/candidate";

interface CandidateEditorModalProps {
  mode: "add" | "edit";
  candidate?: Candidate;
  open: boolean;
  pending?: boolean;
  onClose: () => void;
  onSave: (input: CandidateWriteInput) => Promise<void>;
}

function emptyDraft(): CandidateWriteInput {
  return {
    title: "",
    content: "",
    provenance: "",
    confidence: 0.5,
  };
}

function draftFromCandidate(candidate: Candidate): CandidateWriteInput {
  return {
    title: candidate.title,
    content: candidate.content,
    provenance: candidate.provenance,
    confidence: candidate.confidence,
  };
}

export function CandidateEditorModal({
  mode,
  candidate,
  open,
  pending = false,
  onClose,
  onSave,
}: CandidateEditorModalProps) {
  const titleId = useId();
  const [draft, setDraft] = useState<CandidateWriteInput>(emptyDraft());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    setDraft(mode === "edit" && candidate ? draftFromCandidate(candidate) : emptyDraft());
    setError(null);
  }, [open, mode, candidate]);

  if (!open) {
    return null;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!draft.title.trim() || !draft.content.trim()) {
      setError("Title and content are required.");
      return;
    }
    setError(null);
    try {
      await onSave({
        title: draft.title,
        content: draft.content,
        provenance: draft.provenance?.trim() || undefined,
        confidence: draft.confidence,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
      >
        <h3 className="modal-panel__title" id={titleId}>
          {mode === "add" ? "Add eval" : "Edit eval"}
        </h3>
        <form className="modal-form" onSubmit={(event) => void handleSubmit(event)}>
          <label>
            Title
            <input
              type="text"
              value={draft.title}
              onChange={(event) =>
                setDraft((prev: CandidateWriteInput) => ({ ...prev, title: event.target.value }))
              }
              required
              aria-label="Eval title"
            />
          </label>
          <label>
            Content
            <textarea
              value={draft.content}
              onChange={(event) =>
                setDraft((prev: CandidateWriteInput) => ({ ...prev, content: event.target.value }))
              }
              rows={5}
              required
              aria-label="Eval content"
            />
          </label>
          <label>
            Provenance (optional)
            <input
              type="text"
              value={draft.provenance ?? ""}
              onChange={(event) =>
                setDraft((prev: CandidateWriteInput) => ({ ...prev, provenance: event.target.value }))
              }
              placeholder="logs/session.jsonl:42"
              aria-label="Eval provenance"
            />
          </label>
          <label>
            Confidence
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={draft.confidence ?? 0.5}
              onChange={(event) =>
                setDraft((prev: CandidateWriteInput) => ({
                  ...prev,
                  confidence: Number(event.target.value),
                }))
              }
              aria-label="Eval confidence"
            />
          </label>
          {error ? (
            <p className="warning-banner" role="alert">
              {error}
            </p>
          ) : null}
          <div className="modal-form__actions">
            <button type="submit" className="btn primary" disabled={pending}>
              {mode === "add" ? "Add eval" : "Save changes"}
            </button>
            <button type="button" className="btn ghost" onClick={onClose} disabled={pending}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
