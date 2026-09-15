{{- define "instruction-hub-worker.name" -}}
instruction-hub-worker
{{- end -}}

{{- define "instruction-hub-worker.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "instruction-hub-worker.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "instruction-hub-worker.secretName" -}}
{{- if .Values.secrets.existingSecretName -}}
{{- .Values.secrets.existingSecretName -}}
{{- else -}}
{{- include "instruction-hub-worker.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "instruction-hub-worker.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{- .Values.serviceAccount.name -}}
{{- else if .Values.serviceAccount.create -}}
{{- include "instruction-hub-worker.fullname" . -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{- define "instruction-hub-worker.labels" -}}
app.kubernetes.io/name: {{ include "instruction-hub-worker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "instruction-hub-worker.datadogLabels" -}}
{{- if .Values.observability.datadog.enabled }}
tags.datadoghq.com/env: {{ .Values.observability.datadog.env | quote }}
tags.datadoghq.com/service: {{ .Values.observability.datadog.service | quote }}
tags.datadoghq.com/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "instruction-hub-worker.observabilityEnv" -}}
{{- if .Values.observability.datadog.enabled }}
- name: DD_ENV
  value: {{ .Values.observability.datadog.env | quote }}
- name: DD_SERVICE
  value: {{ .Values.observability.datadog.service | quote }}
- name: DD_VERSION
  value: {{ .Chart.AppVersion | quote }}
- name: DD_TRACE_ENABLED
  value: "true"
- name: DD_TRACE_ANALYTICS_ENABLED
  value: "true"
- name: DD_TRACE_AGENT_URL
  value: {{ .Values.observability.datadog.agentUrl | quote }}
- name: DD_SITE
  value: {{ .Values.observability.datadog.site | quote }}
{{- end }}
{{- if .Values.observability.sentry.enabled }}
- name: SENTRY_DSN
  valueFrom:
    secretKeyRef:
      name: {{ required "observability.sentry.existingSecretName is required when Sentry is enabled" .Values.observability.sentry.existingSecretName | quote }}
      key: {{ .Values.observability.sentry.dsnKey | quote }}
- name: SENTRY_ENVIRONMENT
  value: {{ .Values.observability.environment | quote }}
- name: SENTRY_RELEASE
  value: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "instruction-hub-worker.image" -}}
{{- $digest := required "image.digest is required; use a published chart or the verified release digest" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}{{ fail "image.digest must be sha256 followed by 64 lowercase hex characters" }}{{- end -}}
{{- printf "%s@%s" .Values.image.repository $digest -}}
{{- end -}}

{{- define "instruction-hub-worker.storageEnv" -}}
- name: INSTRUCTION_HUB_STORAGE_BACKEND
  value: {{ .Values.instructionHub.storageBackend | quote }}
{{- if eq .Values.instructionHub.storageBackend "postgres_s3" }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_S3_BUCKET
  value: {{ required "instructionHub.traceObjectS3Bucket is required" .Values.instructionHub.traceObjectS3Bucket | quote }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_S3_PREFIX
  value: {{ .Values.instructionHub.traceObjectS3Prefix | quote }}
{{- else if eq .Values.instructionHub.storageBackend "postgres_azure_blob" }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_AZURE_ACCOUNT_URL
  value: {{ required "instructionHub.traceObjectAzureAccountUrl is required" .Values.instructionHub.traceObjectAzureAccountUrl | quote }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_AZURE_CONTAINER
  value: {{ required "instructionHub.traceObjectAzureContainer is required" .Values.instructionHub.traceObjectAzureContainer | quote }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_PREFIX
  value: {{ .Values.instructionHub.traceObjectPrefix | quote }}
{{- else if eq .Values.instructionHub.storageBackend "postgres_gcs" }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_GCS_BUCKET
  value: {{ required "instructionHub.traceObjectGcsBucket is required" .Values.instructionHub.traceObjectGcsBucket | quote }}
- name: INSTRUCTION_HUB_TRACE_OBJECT_PREFIX
  value: {{ .Values.instructionHub.traceObjectPrefix | quote }}
{{- else }}
{{- fail "storageBackend must be postgres_s3, postgres_azure_blob, or postgres_gcs" }}
{{- end }}
{{- if .Values.instructionHub.postgresCaConfigMapName }}
- name: PGSSLROOTCERT
  value: /etc/pig/postgres-ca/ca.pem
{{- end }}
{{- end -}}

{{- define "instruction-hub-worker.caMount" -}}
{{- if .Values.instructionHub.postgresCaConfigMapName }}
- name: postgres-ca
  mountPath: /etc/pig/postgres-ca
  readOnly: true
{{- end }}
{{- end -}}

{{- define "instruction-hub-worker.caVolume" -}}
{{- if .Values.instructionHub.postgresCaConfigMapName }}
- name: postgres-ca
  configMap:
    name: {{ .Values.instructionHub.postgresCaConfigMapName | quote }}
    items:
      - key: {{ .Values.instructionHub.postgresCaConfigMapKey | quote }}
        path: ca.pem
{{- end }}
{{- end -}}
