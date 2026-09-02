{{- define "workload.name" -}}
{{ required "name is required" .Values.name }}
{{- end -}}

{{- define "workload.labels" -}}
app: {{ include "workload.name" . }}
app.kubernetes.io/name: {{ include "workload.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
