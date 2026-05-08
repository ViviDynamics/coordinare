#!/usr/bin/env python3
"""
Apply CSS design tokens to dashboard.py (059-dashboard-ux Phase A).

ONE-SHOT MIGRATION TOOL — already applied to dashboard.py on branch 059-dashboard-ux.
Do not re-run against a dashboard that already contains CSS custom properties; the
idempotency guard will refuse with an error. Kept for reference and future rollbacks.

- Inserts :root { --color-* } block after <style>
- Replaces all hard-coded hex values in CSS block and JS style= strings
- Skips mermaid.initialize themeVariables and classDef graph strings
"""
import re
import sys
from pathlib import Path

ROOT_BLOCK = """:root {
  --color-bg-base:            #0d1117;
  --color-bg-surface:         #161b22;
  --color-bg-elevated:        #21262d;
  --color-border:             #30363d;
  --color-border-subtle:      #21262d;
  --color-text-primary:       #c9d1d9;
  --color-text-muted:         #8b949e;
  --color-accent-blue:        #58a6ff;
  --color-accent-green:       #3fb950;
  --color-accent-yellow:      #d29922;
  --color-accent-red:         #f85149;
  --color-accent-orange:      #f0883e;
  --color-healthy:            var(--color-accent-green);
  --color-degraded:           var(--color-accent-yellow);
  --color-error:              var(--color-accent-red);
  --color-active:             var(--color-accent-blue);
  --color-bg-healthy:         #1f4a1f;
  --color-bg-degraded:        #4a3a1f;
  --color-bg-error:           #4a1f1f;
  --color-bg-accent:          #1a2a3a;
  --color-bg-subtle:          #1e1e2e;
  --color-ev-progress:        #1a2a1a;
  --color-ev-thinking:        #2a2a1a;
  --color-ev-error:           #2a1a1a;
  --color-ev-output:          #1e1e1e;
  --color-bg-row-hover:       #132035;
  --color-bg-row-selected:    #1b2940;
  --color-bg-pill:            #111827;
  --color-accent-blue-subtle: #58a6ff33;
  --color-bar-track:          #333;
  --color-bar-fill:           #27ae60;
  --color-bar-full:           #e74c3c;
  --color-btn-success:        #238636;
  --color-bg-delete:          #3d1f1f;
  --color-border-delete:      #6e2e2e;
}
"""

# Ordered longest-first to avoid partial hex matches (#58a6ff33 before #58a6ff, etc.)
TOKEN_MAP = [
    ('#58a6ff33', 'var(--color-accent-blue-subtle)'),
    ('#0d1117',   'var(--color-bg-base)'),
    ('#161b22',   'var(--color-bg-surface)'),
    ('#21262d',   'var(--color-bg-elevated)'),
    ('#30363d',   'var(--color-border)'),
    ('#c9d1d9',   'var(--color-text-primary)'),
    ('#8b949e',   'var(--color-text-muted)'),
    ('#58a6ff',   'var(--color-accent-blue)'),
    ('#3fb950',   'var(--color-accent-green)'),
    ('#d29922',   'var(--color-accent-yellow)'),
    ('#f85149',   'var(--color-accent-red)'),
    ('#f0883e',   'var(--color-accent-orange)'),
    ('#1f4a1f',   'var(--color-bg-healthy)'),
    ('#4a3a1f',   'var(--color-bg-degraded)'),
    ('#4a1f1f',   'var(--color-bg-error)'),
    ('#1a2a3a',   'var(--color-bg-accent)'),
    ('#1e1e2e',   'var(--color-bg-subtle)'),
    ('#1a2a1a',   'var(--color-ev-progress)'),
    ('#2a2a1a',   'var(--color-ev-thinking)'),
    ('#2a1a1a',   'var(--color-ev-error)'),
    ('#1e1e1e',   'var(--color-ev-output)'),
    ('#132035',   'var(--color-bg-row-hover)'),
    ('#1b2940',   'var(--color-bg-row-selected)'),
    ('#111827',   'var(--color-bg-pill)'),
    ('#27ae60',   'var(--color-bar-fill)'),
    ('#e74c3c',   'var(--color-bar-full)'),
    ('#238636',   'var(--color-btn-success)'),
    ('#3d1f1f',   'var(--color-bg-delete)'),
    ('#6e2e2e',   'var(--color-border-delete)'),
    ('#333',      'var(--color-bar-track)'),
]


def apply_tokens(text: str) -> str:
    for hex_val, token in TOKEN_MAP:
        text = text.replace(hex_val, token)
    return text


def main() -> None:
    src = Path('src/coordinare/dashboard.py')
    content = src.read_text()

    # Guard: refuse to run if tokens have already been applied.
    if '--color-bg-base' in content:
        print("ERROR: CSS tokens already present in dashboard.py. Refusing to re-apply.", file=sys.stderr)
        sys.exit(1)

    # --- Step 1: Insert :root block right after <style>\n ---
    style_open = '<style>\n'
    assert style_open in content, "Could not find <style> tag"
    content = content.replace(style_open, style_open + ROOT_BLOCK, 1)

    # --- Step 2: Protect mermaid sections from token replacement ---
    # Protect mermaid.initialize({...}); — contains JSON-style hex that must stay literal
    mermaid_re = re.compile(
        r'(mermaid\.initialize\(\{.*?\}\);)',
        re.DOTALL,
    )
    # Protect classDef lines inside JS string arrays — mermaid graph syntax
    classdef_re = re.compile(
        r"('  classDef [^']*')",
        re.MULTILINE,
    )

    placeholders: dict[str, str] = {}
    counter = [0]

    def protect(m: re.Match) -> str:
        key = f'\x00PROTECTED_{counter[0]}\x00'
        counter[0] += 1
        placeholders[key] = m.group(0)
        return key

    content = mermaid_re.sub(protect, content)
    content = classdef_re.sub(protect, content)

    # --- Step 3: Apply token replacements to the rest of the file ---
    content = apply_tokens(content)

    # --- Step 4: Restore protected sections ---
    for key, original in placeholders.items():
        content = content.replace(key, original)

    # --- Step 5: Write output ---
    src.write_text(content)
    print(f"Done. {len(placeholders)} section(s) protected from replacement.")

    # --- Step 6: Verify no raw hex values remain outside protected zones ---
    verify = content
    # Remove protected sections for verification
    for original in placeholders.values():
        verify = verify.replace(original, '')
    # Check :root block itself (expected to still have hex)
    root_end = verify.find('}\n* {')
    if root_end > 0:
        verify = verify[root_end + 2:]

    remaining = re.findall(r'#[0-9a-fA-F]{3,8}\b', verify)
    if remaining:
        # rgba() values and mermaid active classDef (#1f3a5f, #ffffff) are expected
        known_ok = {'#1f3a5f', '#ffffff'}
        unexpected = [h for h in remaining if h.lower() not in known_ok]
        if unexpected:
            print(f"WARNING: {len(unexpected)} unreplaced hex value(s) found:", file=sys.stderr)
            for h in sorted(set(unexpected)):
                print(f"  {h}", file=sys.stderr)
    else:
        print("Verification: no unexpected hex values remain outside :root block and protected sections.")


if __name__ == '__main__':
    main()
