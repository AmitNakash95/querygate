{{/*
Chart name, truncated for Kubernetes name limits.
*/}}
{{- define "querygate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name — includes the release name unless it's already
part of the chart name, or fullnameOverride is set.
*/}}
{{- define "querygate.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "querygate.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "querygate.labels" -}}
helm.sh/chart: {{ include "querygate.chart" . }}
{{ include "querygate.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "querygate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "querygate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "querygate.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "querygate.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret containing API keys, the Vault token, and every ${VAR}
connections.yaml references — either the chart-rendered one or an
operator-supplied existing Secret.
*/}}
{{- define "querygate.secretName" -}}
{{- if .Values.secrets.existingSecretName }}
{{- .Values.secrets.existingSecretName }}
{{- else }}
{{- include "querygate.fullname" . }}
{{- end }}
{{- end }}
