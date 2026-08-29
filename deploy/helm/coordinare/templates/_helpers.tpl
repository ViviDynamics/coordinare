{{/* Chart name, overridable. */}}
{{- define "coordinare.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully-qualified release name. */}}
{{- define "coordinare.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "coordinare.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "coordinare.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "coordinare.labels" -}}
app.kubernetes.io/name: {{ include "coordinare.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: coordinare
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "coordinare.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coordinare.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "coordinare.serviceAccountName" -}}
{{- default (include "coordinare.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{/*
The Service's in-cluster DNS name.

Single source for both the Service object and the trusted-host entry that makes
it usable. If these two ever disagree the dashboard returns 403 for a reason
nobody can see, so they are derived rather than written twice.
*/}}
{{- define "coordinare.serviceDNS" -}}
{{- printf "%s.%s.svc.cluster.local" (include "coordinare.fullname" .) .Release.Namespace -}}
{{- end -}}

{{/*
Trusted dashboard hosts.

Spec 144 leaves this list empty by default: the guard refuses any request whose
Host is not loopback. The chart adds the Service's own DNS name and the short
forms Kubernetes resolves it by, because otherwise the Service it ships would
403 on every request.

Deliberately narrow: exact hostnames only. A wildcard here would hand the guard
away entirely, and a test asserts none appears.
*/}}
{{- define "coordinare.trustedHosts" -}}
{{- $auto := list (include "coordinare.serviceDNS" .) (printf "%s.%s" (include "coordinare.fullname" .) .Release.Namespace) (include "coordinare.fullname" .) -}}
{{- concat $auto (.Values.dashboard.trustedHosts | default list) | uniq | toYaml -}}
{{- end -}}

{{/*
The replica guard.

Coordinare's state is a single-process JSON snapshot (state_store.py). A second
replica does not share that state — it races on the same file and corrupts it.
So this is a correctness constraint, not a tuning default, and the render fails
rather than obeying.

Failing here rather than documenting it is deliberate: `--set replicaCount=3` is
exactly what an operator types when they assume coordinare scales horizontally,
and this is the one moment they are guaranteed to read the reason.
*/}}
{{- define "coordinare.checkReplicas" -}}
{{- if and (ne (int .Values.replicaCount) 1) (not .Values.allowUnsafeMultiReplica) -}}
{{- fail (printf "\n\nreplicaCount is %d, but coordinare supports exactly 1.\n\nCoordinare's state is a single-process JSON snapshot on one volume. A second\nreplica does not divide the work; it races on that file and will corrupt or\nlose state. This is a correctness constraint, not a performance default.\n\nIf you understand that and still need it, set allowUnsafeMultiReplica=true.\n" (int .Values.replicaCount)) -}}
{{- end -}}
{{- end -}}
