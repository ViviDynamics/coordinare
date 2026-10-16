# Implementation plan

Move the 083 scanner implementation into the performer package. Before backend
startup, validate the versioned request and scan the performer checkout off the
async event loop. Keep findings in runtime session state, feed the backend
prompt and attach the report to terminal responses, including repeated polls.

Remove the daemon scanner entry point. Dispatch sends the changed path list
from its existing GitHub diff read. The monitor validates response evidence
before stage advancement and applies the existing severity and routing gate.
Workflow selection is operator controlled, never inferred from a prose report.

Enforce the transport boundary in WorkspaceManager itself so legacy callers
cannot clone for a remote transport. Keep subprocess checkout and API project
context behavior. Shrink the mypy exception list after removing the scanner.
