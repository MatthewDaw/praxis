import { useState } from "react";

interface InlineRenameProps {
  /** The current name, shown prefilled and selected for quick replacement. */
  initialValue: string;
  /** Accessible label for the text input (e.g. "New organization name"). */
  label: string;
  /**
   * Persist the new name. Throwing surfaces the message inline and keeps the
   * editor open; resolving is the parent's cue to close edit mode.
   */
  onSubmit: (value: string) => Promise<void>;
  /** Leave edit mode without saving (Esc, Cancel, or blur-to-cancel). */
  onCancel: () => void;
}

/**
 * Inline rename editor shared by the org/space/snapshot switchers: a text input
 * the dropdown is swapped for while editing. Enter (or Save) submits the trimmed
 * value; Escape (or Cancel) aborts. Errors from {@link onSubmit} render in place
 * so a 409 (snapshot name taken) or 403 stays visible without closing the editor.
 */
export function InlineRename({ initialValue, label, onSubmit, onCancel }: InlineRenameProps) {
  const [value, setValue] = useState(initialValue);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const next = value.trim();
    if (!next) {
      setError("Name is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="inline-rename" onSubmit={handleSubmit}>
      <div className="inline-rename__row">
        <input
          className="inline-rename__input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              onCancel();
            }
          }}
          aria-label={label}
          autoFocus
          // Select the existing text so a quick retype replaces it wholesale.
          onFocus={(e) => e.currentTarget.select()}
        />
        <button type="submit" className="inline-rename__save" disabled={submitting}>
          {submitting ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="link-button inline-rename__cancel"
          onClick={onCancel}
          disabled={submitting}
        >
          Cancel
        </button>
      </div>
      {error ? <p className="inline-rename__error">{error}</p> : null}
    </form>
  );
}
