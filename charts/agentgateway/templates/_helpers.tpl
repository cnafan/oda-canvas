{{- define "agentgateway.testMcpServer.name" -}}
{{ .Release.Name }}-test-mcp-server
{{- end -}}

{{- define "agentgateway.labels" -}}
app.kubernetes.io/part-of: agentgateway
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
