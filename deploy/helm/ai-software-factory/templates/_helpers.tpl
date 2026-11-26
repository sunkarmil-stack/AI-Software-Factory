{{- define "asf.labels" -}}
app.kubernetes.io/part-of: ai-software-factory
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "asf.image" -}}
{{- required "image.digest is required (deploy by digest)" .Values.image.digest | printf "%s@%s" .Values.image.repository -}}
{{- end -}}
{{- define "asf.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  fsGroup: 10001
  seccompProfile: { type: RuntimeDefault }
{{- end -}}
{{- define "asf.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: ["ALL"] }
{{- end -}}
{{- define "asf.env" -}}
- { name: FACTORY_CONFIG, value: /etc/factory/factory.yaml }
- { name: FACTORY_DATABASE_URL, value: {{ required "factory.databaseUrl is required" .Values.factory.databaseUrl | quote }} }
- { name: FACTORY_HOME, value: /var/factory }
- { name: OTEL_SERVICE_NAME, value: ai-software-factory }
{{- end -}}
