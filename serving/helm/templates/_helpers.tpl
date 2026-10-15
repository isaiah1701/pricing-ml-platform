{{- define "airbnb.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "airbnb.airflowProbe" -}}
{{- if eq . "api-server" }}
httpGet:
  path: /api/v2/version
  port: http
{{- else if eq . "scheduler" }}
httpGet:
  path: /health
  port: 8974
{{- else }}
exec:
  command:
    - airflow
    - jobs
    - check
    - --job-type
    - {{ if eq . "dag-processor" }}DagProcessorJob{{ else }}TriggererJob{{ end }}
    - --local
{{- end }}
{{- end }}

{{- define "airbnb.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "airbnb.labels" -}}
app.kubernetes.io/name: {{ include "airbnb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "airbnb.selectorLabels" -}}
app.kubernetes.io/name: {{ include "airbnb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "airbnb.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "airbnb.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- required "serviceAccount.name is required when create=false" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}
