# 284 Persistent daemon config

The dashboard must save configuration beside daemon state so operators can manage
symphonies without infrastructure changes. Workspace storage and env cache changes
are out of scope.

Use `<state.mountPath>/config/config.yaml` on the existing state claim. Config and
state share one writer and one lifecycle. A separate claim would complicate
StatefulSet upgrades and backup without adding isolation that this daemon needs.

An init container using the daemon image imports the raw chart ConfigMap on first
boot only. Store its SHA256 and section fingerprints in `seed.yaml`. Preserve
placeholders and null values, never expand credentials into persisted YAML.
Reject literal secret fields and secret environment entries for persisted config.
All dashboard saves retain existing validation, concurrency and reload behavior.

At boot and on Config page reads, compare the mounted seed with the recorded
baseline. Show changed section names only, never values. Also distinguish live
edits from seed changes. Missing or corrupt metadata means unknown provenance;
never silently establish a new baseline or overwrite an existing config.

An operator may explicitly create `config/reseed` on the state claim and restart
the Pod. The init step validates the seed secret policy, atomically replaces the
live file, records the new baseline, then removes the marker. A failed step leaves
the marker for retry. This resets all dashboard edits and requires an operator
backup. Normal restarts and upgrades do not replace live config.

`configPersistence.enabled` defaults to true. Disabling restores the original
read only ConfigMap path and keeps the PVC copy available for reenable. Existing
reload triggers and restart required save messages remain; no new reload system.
