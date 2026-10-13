# Browser appearance state

Preference: light | dark | system. Default system, including invalid saved values.
Effective appearance: light | dark, derived from preference and OS media query.
Storage key: coordinare.appearance. No secrets, server persistence, or API fields.
Apply effective appearance to document.documentElement.dataset.theme and
style.colorScheme. Preference selection is synchronized to the appearance select.
Dispatch coordinare:appearance when effective appearance changes after bootstrap.
Observe OS changes only in System, and storage events to synchronize browser tabs.

Desktop drawer preference: boolean, default false (expanded). Storage key:
coordinare.navigationCollapsed. Apply dataset.navigationCollapsed before paint.
Desktop widths are 224px expanded and 80px collapsed; mobile menu-open state is
in-memory only and does not change the desktop preference. Storage failures
fall back to an expanded drawer but never disable its toggle.
