# Contract: `load_test_env`

**Module**: `src/coordinare/services/test_env_loader.py` (new)

## Signature

```python
def load_test_env(cfg: TestEnvConfig, *, repo_root: Path) -> dict[str, str]: ...
```

A pure function: given a validated `TestEnvConfig` and the cloned repo root, returns the
parsed `KEY → literal value` mapping. No side effects beyond reading the named file. No
shell execution.

## Source resolution

| `cfg` field | Behavior |
|-------------|----------|
| `host_path` set | Read the absolute host path directly. |
| `repo_path` set | Resolve under `repo_root`; re-assert containment (reject `..` / absolute) even though config-load already checked — defense in depth. |

The same function is reused for the agent-discovered fallback path (passed as a
repo-relative `repo_path`-equivalent against `repo_root`).

## Parsing rules (dotenv-style, values literal)

- Split each line on the **first** `=` → `(key, value)`.
- Skip blank lines and lines whose first non-whitespace char is `#`.
- Strip **one** layer of matching surrounding quotes (`"..."` or `'...'`) from the value.
- Ignore a leading `export ` before the key.
- **No** shell execution, command substitution, or inter-variable interpolation:
  a value `$(rm -rf /)` is returned as the literal string `"$(rm -rf /)"`.
- `=` inside the value is preserved (only the first `=` splits).
- CRLF line endings tolerated (trailing `\r` stripped).

## Returns

`dict[str, str]` — plain mapping. Empty file → empty dict (only when the file exists and
is genuinely empty).

## Errors

| Condition | Result |
|-----------|--------|
| Configured file missing/unreadable | Raise a clear coordinare-side error naming the **resolved path** and the **field** (`repo_path` / `host_path` / discovered) that pointed at it. NOT a silent empty dict. |
| `repo_path` escapes `repo_root` | Raise containment error (mirrors config-load rule). |

## Secret handling

- Every returned value is secret-like. Callers route it through the redacted `secrets`
  channel; the loader itself does **no** logging of values.
- Any diagnostic the loader emits names only keys and the source — never values.

## Tests (unit)

- Quotes (single/double, one layer only), leading `export `, `#` comments, blank lines,
  `=` inside value, CRLF.
- Containment rejection of `../` and absolute `repo_path`.
- Host vs. repo source resolution.
- Missing-file error names path + field.
- No shell execution: a `$(...)` / `` `cmd` `` value returned literally.
