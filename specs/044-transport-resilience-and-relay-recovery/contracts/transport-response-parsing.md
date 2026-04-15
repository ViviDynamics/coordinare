# Transport Response Parsing Contract

## Protocol

The subprocess transport uses line-delimited JSON over stdout. Each valid protocol response is a single JSON object occupying one complete line (terminated by `\n`).

## Resilient Parsing Rules (044)

1. Read the response buffer (may contain multiple lines)
2. For each line:
   a. Strip whitespace
   b. Skip empty lines
   c. Attempt `json.loads(line)` — if it fails, this is non-JSON noise → log at debug, skip
   d. If JSON parse succeeds, attempt `ProtocolResponse.model_validate_json(line)`
   e. If Pydantic validation succeeds → return the response
   f. If Pydantic validation fails → raise `TransportError("Valid JSON but invalid protocol response")` immediately (this is a real schema error, not noise)
3. If no valid JSON found after all lines → raise `TransportError("No valid JSON response in output")`

## Backward Compatibility

- Valid single-line JSON responses parse identically to before (step 2c succeeds on first line)
- The only behavioral change is: non-JSON lines preceding a valid JSON line are now skipped instead of causing an immediate error
