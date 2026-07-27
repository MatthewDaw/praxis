// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobQuestionPanel } from "./JobQuestionPanel";

afterEach(cleanup);

describe("JobQuestionPanel (R79)", () => {
  it("renders the question text immediately next to the reply control", () => {
    render(
      <JobQuestionPanel
        jobId="job-1"
        question="Which service: A or B?"
        onReply={vi.fn()}
      />,
    );

    const question = screen.getByTestId("job-question");
    const reply = screen.getByTestId("job-reply-control");
    expect(question).toHaveTextContent("Which service: A or B?");
    // "next to" -- the two are adjacent siblings in the same panel, question first.
    expect(question.parentElement).toBe(reply.parentElement);
    expect(question.nextElementSibling).toBe(reply);
  });

  it("renders only the reply control when the job has no open question", () => {
    render(<JobQuestionPanel jobId="job-1" question={null} onReply={vi.fn()} />);

    expect(screen.queryByTestId("job-question")).toBeNull();
    expect(screen.getByTestId("job-reply-control")).toBeInTheDocument();
  });

  it("submits the reply message for the given job id", async () => {
    const onReply = vi.fn();
    render(
      <JobQuestionPanel jobId="job-42" question="A or B?" onReply={onReply} />,
    );

    await userEvent.type(screen.getByLabelText("Reply"), "Use B");
    await userEvent.click(screen.getByRole("button", { name: "Reply" }));

    expect(onReply).toHaveBeenCalledWith("job-42", "Use B");
  });
});
