# speckit.contracts

**Purpose**: Maintenance tool that compares the `specs/contracts/*.md` field registries against the current source code schemas. Reports drift in both directions so a spec author can keep contracts in sync.

## When to use

Run when you want to audit whether the contract files match the current codebase — for example, after a large refactor or before a release. This is a developer-facing tool, not part of the normal speckit pipeline.

## Steps

1. **Load contract registries**

   For each `.md` file in `specs/contracts/`:
   - Read the file.
   - Find the `## Field Registry` section.
   - Parse every markdown table within that section.
   - Extract field names from the **first column** of each table row (skip header rows).
   - Build a registry: `{contract_file: [field_name, ...]}`

   If no contract files exist, report "No contract files found in specs/contracts/" and stop.

2. **Locate corresponding Pydantic models**

   For each contract file, identify the related Pydantic model class in the source code. The mapping is:
   - `dispatch-payload.md` → `Score` in `agent/performer/src/performer/models.py`
   - Other contracts → infer from the contract filename or the `**Boundary**` line at the top of the contract file.

   For each identified model class, use Grep to find all `field_name: ...` field declarations in the model.

3. **Compute drift**

   For each contract:
   - **Fields in code but absent from contract**: fields declared in the Pydantic model that do not appear in the contract's Field Registry.
   - **Fields in contract but absent from code**: field names in the contract that have no corresponding declaration in the Pydantic model.

4. **Output diff-style summary**

   ```
   Contract: specs/contracts/dispatch-payload.md  ↔  Score (agent/performer/src/performer/models.py)

   In code, not in contract:
     + new_field_name       (line 42 of models.py)

   In contract, not in code:
     - old_field_name       (registered in dispatch-payload.md)

   Unchanged: 14 fields match
   ```

   If both sides are in sync, output: `All contracts match their source models. No drift detected.`

5. **Guidance**

   For each drift item, suggest one of:
   - "Add `{field}` to the contract registry in `{contract_file}` (run `speckit.contracts` again after updating)"
   - "Remove `{field}` from the contract or add it to the Pydantic model"

## Notes

- This tool is **read-only** — it never modifies any file.
- It does not block the speckit pipeline. Run it manually when auditing contract accuracy.
- The Pydantic model fields are found by grepping for `^\s+{field_name}\s*:` patterns. Inherited or computed fields that do not appear in the model body are not detected.
