# Plan

Add an explicit git-auth capability flag to FakeGitHubService, default enabled for compatibility. Disable it only when run_board uses stub dispatch. Both token accessors return empty in that mode, preserving the existing no-token guards in daemon and board preflight. Prove zero SHA fetches through an actual optimizer search and verify the default and disabled token accessors.

Analysis: stub dispatch already performs all git work directly against the local bare repository; its gate/model dimensions intentionally carry no real-mode signal. Real mode keeps the default fake token and existing fake-server wiring. No schema, production authentication, or rebase algorithm changes are needed.
