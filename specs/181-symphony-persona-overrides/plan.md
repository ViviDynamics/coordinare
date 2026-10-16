# Implementation plan

Use the existing app.js symphony form and Config safeguards. Mount that form only
inside Config's Symphonies tab. Extend personas.js with symphony presentation and
state handling using personaSelector, personaPanel, personaEditor and personaMount.
Use the existing persona and symphony APIs without new endpoints or snapshot fields.
Wire nonempty symphony instructions into effective_config, preserving other roles
and scope metadata. Apply the same resolution after global hot reload at dispatch. Use the existing dry run route before PUT of the reviewed body.

Write unit, shipped JavaScript node and browser tests first. Verify lint, mypy,
ruff baseline, relevant pytest and node checks. Collect browser tests only.
