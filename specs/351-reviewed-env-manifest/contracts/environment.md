# Environment contract v1

Authoritative Python schema lives in the existing service-inference shared wheel's
`environment.models`; daemon and performer import the same models. Unknown keys and
versions refuse. Canonical JSON uses sorted keys, compact separators and UTF-8; SHA256
covers all model fields. `${NAME}` is a runtime env reference; `{cache}`, `{repo}` and
`{scratch}` are the only root substitutions. argv is never implicit shell code.

Discovery replies are either `{"tool":"read_file","path":"relative/path"}`,
`{"tool":"list_files"}`, or `{"manifest":{...}}`. Reviewer replies use the same
read tools or `{"assessment":{...}}`; review starts with a fresh conversation.
Missing evidence, failed citations, revision verdicts and limits raise an explicit hold.

Dispatch metadata carries `environment_manifest`, `environment_assessment` and
`environment_manifest_digest`. No installer cache copy is authoritative. Scripts kept
in daemon snapshot embed the accepted contract for clean verification. Runtime records
exit status independently of child output. Cache consumers need frozen provenance;
all required applicable check outcomes must be pass. Missing schema capability holds.
