# 180 Persona Markdown editor

Config is the only global persona editing surface. Config and the read only Personas page select one persona through an accessible rail, with the selection in the URL. Keep history and stage metadata.

The reusable editor provides highlighted Markdown source, a safe preview, formatting tools, indentation and the platform save shortcut. Wide layouts show source and preview together; narrow layouts offer Write and Preview. The source occupies at least 60vh at 1280 by 800.

Keep exact instruction strings, including whitespace and line endings, when saving. Use the shared Config review, unsaved guard, conflict reapply and content hash behavior. Reset requires review explaining that it restores the built in default and discards local edits. Use the shared atomic YAML writer and validate before writing. No new dependencies, network resources or executable operator markup.

Verify selector navigation, dirty markers, review and conflict behavior, exact round trips, hostile Markdown, keyboard access, both themes and 375px layout. Browser verification is supplied for the operator to run because this sandbox cannot bind sockets.
