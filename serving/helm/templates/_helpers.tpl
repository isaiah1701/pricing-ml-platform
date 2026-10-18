{{- define "airbnb.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "airbnb.image" -}}
{{- $repository := required "image.repository is required" .repository -}}
{{- if .digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .digest) -}}
{{- fail "image.digest must be a sha256 digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository .digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (required "image.tag is required" .tag) -}}
{{- end -}}
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
    - python
    - -c
    - |
      import logging, os, socket, sys
      import psycopg2
      connection = psycopg2.connect(os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"].replace("postgresql+psycopg2:", "postgresql:", 1), connect_timeout=3, options="-c statement_timeout=3000")
      cursor = connection.cursor()
      cursor.execute("SELECT EXISTS (SELECT 1 FROM job WHERE job_type = %s AND hostname = %s AND state = 'running' AND latest_heartbeat > now() - interval '120 seconds')", ("{{ if eq . "dag-processor" }}DagProcessorJob{{ else }}TriggererJob{{ end }}", socket.gethostname()))
      healthy = cursor.fetchone()[0]
      cursor.close()
      connection.close()
      if not healthy:
          logging.warning("Airflow component heartbeat is absent or stale")
      sys.exit(0 if healthy else 1)
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
