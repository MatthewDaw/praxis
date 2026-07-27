import { useState } from "react";

/**
 * The job-view half of R79: a blocked-on-question job's question text,
 * rendered directly adjacent to the reply control (the R28 mailbox message
 * box) so the operator sees what they are answering. `question` is `null`/
 * `undefined` for a job that has never paused on a question -- the panel
 * then renders only the reply control, matching every other remote job.
 */
export interface JobQuestionPanelProps {
  jobId: string;
  question?: string | null;
  onReply: (jobId: string, message: string) => void | Promise<void>;
}

export function JobQuestionPanel({ jobId, question, onReply }: JobQuestionPanelProps) {
  const [message, setMessage] = useState("");

  return (
    <div className="job-question-panel">
      {question ? (
        <p className="job-question-panel__question" data-testid="job-question">
          {question}
        </p>
      ) : null}
      <form
        className="job-question-panel__reply"
        data-testid="job-reply-control"
        onSubmit={(event) => {
          event.preventDefault();
          void onReply(jobId, message);
          setMessage("");
        }}
      >
        <input
          aria-label="Reply"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
        />
        <button type="submit">Reply</button>
      </form>
    </div>
  );
}
