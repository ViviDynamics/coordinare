# Symphony persona overrides

Editing lives in Config, under Symphonies. The Symphony detail page shows status,
settings and a read only persona summary with a matching Config link.

1. Both views use the persona API list and the shared selector and Markdown renderer.
2. Inherited instructions show the effective global text. Config offers customization
   from a copy or blank, comparison, and confirmed revert to inheritance.
3. Overrides replace the global instructions for that role. Other roles and scope
   behavior remain inherited. Empty or whitespace only instructions mean inheritance.
4. Config uses one Save, a dirty marker, leave protection, reviewed snapshots,
   dry run validation, and the existing versioned symphony update route.
5. Source text remains exact, including whitespace and line endings. Unedited saves
   send no request. Secrets retain the existing masking and review behavior.
6. Light and dark themes and a 375px viewport remain usable. No browser can be run
   in this workspace; browser tests must be collected and handed off for execution.
