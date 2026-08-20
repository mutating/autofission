{{- define "autofission.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "autofission.fullname" -}}
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

{{- define "autofission.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "autofission.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "autofission.selectorLabels" -}}
app.kubernetes.io/name: {{ include "autofission.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "autofission.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "autofission.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when serviceAccount.create=false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "autofission.controllerPriorityClassName" -}}
{{- default (printf "%s-controller" (include "autofission.fullname" .)) .Values.priorityClasses.controller.name }}
{{- end }}

{{- define "autofission.runtimePriorityClassName" -}}
{{- default (printf "%s-runtime" (include "autofission.fullname" .)) .Values.priorityClasses.runtime.name }}
{{- end }}

