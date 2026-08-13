"""Spec 134 — board-simulation benchmark (Phase 1 evaluation substrate).

Self-contained harness that runs the real coordinare daemon against an in-process
``FakeGitHubService`` and emits a structured run artifact. Nothing in this package
is imported by production coordinare paths — it exists only to exercise coordinare
end-to-end for the benchmark program (specs 134-137).
"""
