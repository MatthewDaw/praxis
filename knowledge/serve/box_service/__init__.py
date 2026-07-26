"""Box-service seams: injectable abstractions over the real Claude Code
background-session daemon and lock-wrapped host commands, so job-control
behavior is asserted against a fake in unit tests rather than a live session
or a real host lock (see R83)."""
