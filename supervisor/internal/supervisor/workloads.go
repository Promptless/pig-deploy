package supervisor

import "encoding/json"

func SecretRefs(s DeploymentSpec) []SecretRef {
	refs := []SecretRef{s.Hosted.InstallTokenSecretRef, s.Storage.Postgres.DSNSecretRef}
	if s.Storage.Postgres.MigrationDSNSecretRef != nil {
		refs = append(refs, *s.Storage.Postgres.MigrationDSNSecretRef)
	}
	if s.Analysis.Model.APIKeySecretRef != nil {
		refs = append(refs, *s.Analysis.Model.APIKeySecretRef)
	}
	return refs
}
func envValue(name, value string) any { return Obj{"name": name, "value": value} }
func envSecret(name string, ref SecretRef) any {
	return Obj{"name": name, "valueFrom": Obj{"secretKeyRef": asObj(ref)}}
}
func podTemplate(s DeploymentSpec, r Release, hash, name string) Obj {
	m := s.Analysis.Model
	quiet := pythonFloat(s.Analysis.QuietWindowHours)
	values := [][2]string{
		{"RUNTIME_BASE_URL", s.Hosted.RuntimeURL}, {"CONFIG_HASH", hash}, {"WORKER_VERSION", r.Version}, {"CHART_VERSION", r.Version},
		{"ANALYSIS_ACTIVATION_AT", s.Analysis.ActivationAt}, {"ANALYSIS_QUIET_WINDOW_HOURS", quiet}, {"ANALYSIS_MIRROR_ROOT", "/tmp/analysis-mirrors"},
		{"ANALYSIS_MODEL_PROVIDER", m.Provider}, {"ANALYSIS_MODEL_AUTHENTICATION", m.Authentication}, {"ANALYSIS_MODEL_BASE_URL", m.BaseURL}, {"ANALYSIS_MODEL_NAME", m.Name},
	}
	extra := []any{}
	switch {
	case s.Storage.S3 != nil:
		x := s.Storage.S3
		values = append(values, [2]string{"STORAGE_BACKEND", "postgres_s3"}, [2]string{"TRACE_OBJECT_S3_BUCKET", x.Bucket}, [2]string{"TRACE_OBJECT_S3_PREFIX", x.Prefix})
		extra = append(extra, envValue("AWS_REGION", x.Region))
	case s.Storage.AzureBlob != nil:
		x := s.Storage.AzureBlob
		values = append(values, [2]string{"STORAGE_BACKEND", "postgres_azure_blob"}, [2]string{"TRACE_OBJECT_AZURE_ACCOUNT_URL", x.AccountURL}, [2]string{"TRACE_OBJECT_AZURE_CONTAINER", x.Container}, [2]string{"TRACE_OBJECT_PREFIX", x.Prefix})
	case s.Storage.GCS != nil:
		x := s.Storage.GCS
		values = append(values, [2]string{"STORAGE_BACKEND", "postgres_gcs"}, [2]string{"TRACE_OBJECT_GCS_BUCKET", x.Bucket}, [2]string{"TRACE_OBJECT_PREFIX", x.Prefix})
	}
	env := []any{}
	for _, v := range values {
		env = append(env, envValue("PIG_"+v[0], v[1]))
	}
	env = append(env, extra...)
	env = append(env, envSecret("PIG_INSTALL_TOKEN", s.Hosted.InstallTokenSecretRef), envSecret("PIG_CUSTOMER_POSTGRES_DSN", s.Storage.Postgres.DSNSecretRef))
	if m.APIKeySecretRef != nil {
		env = append(env, envSecret("PIG_ANALYSIS_MODEL_API_KEY", *m.APIKeySecretRef))
	}
	mounts := []any{Obj{"name": "tmp", "mountPath": "/tmp"}}
	volumes := []any{Obj{"name": "tmp", "emptyDir": Obj{}}}
	if ref := s.Storage.Postgres.CAConfigMapRef; ref != nil {
		env = append(env, envValue("PGSSLROOTCERT", "/etc/pig/postgres-ca/ca.pem"))
		mounts = append(mounts, Obj{"name": "postgres-ca", "mountPath": "/etc/pig/postgres-ca", "readOnly": true})
		volumes = append(volumes, Obj{"name": "postgres-ca", "configMap": Obj{"name": ref.Name, "items": []any{Obj{"key": ref.Key, "path": "ca.pem"}}}})
	}
	labels := Obj{}
	for k, v := range s.PodLabels {
		labels[k] = v
	}
	labels["app.kubernetes.io/name"] = name + "-analyzer"
	labels["app.kubernetes.io/component"] = "analyzer"
	readiness := "/healthz"
	if contains(r.Requirements.Capabilities, "storage-readiness-v1") {
		readiness = "/readyz"
	}
	return Obj{
		"metadata": Obj{"labels": labels, "annotations": Obj{
			"governance.promptless.ai/config-hash": hash,
			// Datadog agents with containerCollectAll disabled tail only annotated containers.
			"ad.datadoghq.com/analyzer.logs": `[{"source":"python","service":"pig-analyzer"}]`,
		}},
		"spec": Obj{
			"serviceAccountName": s.ServiceAccountName, "nodeSelector": asObj(s.NodeSelector), "automountServiceAccountToken": true,
			"securityContext":               Obj{"runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": Obj{"type": "RuntimeDefault"}},
			"terminationGracePeriodSeconds": 120, "volumes": volumes,
			"containers": []any{Obj{
				"name": "analyzer", "image": r.AnalyzerImage, "args": []any{"serve"}, "env": env,
				"securityContext": Obj{"readOnlyRootFilesystem": true, "allowPrivilegeEscalation": false, "capabilities": Obj{"drop": []any{"ALL"}}},
				"resources":       Obj{"requests": Obj{"cpu": "500m", "memory": "1Gi"}, "limits": Obj{"memory": "2Gi"}},
				"ports":           []any{Obj{"name": "http", "containerPort": 8080}}, "volumeMounts": mounts,
				"readinessProbe": Obj{"httpGet": Obj{"path": readiness, "port": "http"}, "periodSeconds": 10},
				"startupProbe":   Obj{"httpGet": Obj{"path": "/healthz", "port": "http"}, "periodSeconds": 5, "failureThreshold": 36},
				"livenessProbe":  Obj{"httpGet": Obj{"path": "/healthz", "port": "http"}, "initialDelaySeconds": 30},
			}},
		},
	}
}
func metadata(d Obj, name string) Obj {
	p := child(d, "metadata")
	return Obj{"name": name, "namespace": p["namespace"], "labels": Obj{"governance.promptless.ai/deployment": p["name"]}, "ownerReferences": []any{Obj{"apiVersion": d["apiVersion"], "kind": "PIGDeployment", "name": p["name"], "uid": p["uid"], "controller": true}}}
}
func AnalyzerResources(d Obj, s DeploymentSpec, r Release, hash string) []Obj {
	name := str(child(d, "metadata"), "name") + "-analyzer"
	labels := Obj{"app.kubernetes.io/name": name, "app.kubernetes.io/component": "analyzer"}
	return []Obj{
		{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(d, name), "spec": Obj{"replicas": 1, "strategy": Obj{"type": "Recreate"}, "selector": Obj{"matchLabels": labels}, "template": podTemplate(s, r, hash, str(child(d, "metadata"), "name"))}},
		{"apiVersion": "v1", "kind": "Service", "metadata": metadata(d, name), "spec": Obj{"type": "ClusterIP", "selector": labels, "ports": []any{Obj{"name": "http", "port": 8080, "targetPort": "http"}}}},
	}
}

func JobResource(d Obj, s DeploymentSpec, r Release, digest, hash, phase string, attempt int, transition string) Obj {
	token := CanonicalDigest([]any{child(d, "metadata")["uid"], digest, hash, phase, attempt, transition})[:16]
	name := "pig-" + phase + "-" + token
	template := podTemplate(s, r, hash, str(child(d, "metadata"), "name"))
	labels := child(template, "metadata", "labels")
	labels["governance.promptless.ai/job"] = name
	labels["app.kubernetes.io/component"] = "maintenance"
	child(template, "spec")["restartPolicy"] = "Never"
	container := object(items(child(template, "spec")["containers"])[0])
	command := phase
	if phase == "migration" {
		command = "supervised-migrate"
	}
	container["args"] = []any{command}
	env := items(container["env"])
	if (phase == "preflight" || phase == "migration") && s.Storage.Postgres.MigrationDSNSecretRef != nil {
		env = append(env, envSecret("PIG_MIGRATION_POSTGRES_DSN", *s.Storage.Postgres.MigrationDSNSecretRef))
	}
	requirements, _ := json.Marshal(r.Requirements)
	container["env"] = append(env, envValue("PIG_RELEASE_DIGEST", digest), envValue("PIG_REQUIREMENTS", string(requirements)))
	for _, key := range []string{"ports", "readinessProbe", "livenessProbe", "startupProbe"} {
		delete(container, key)
	}
	return Obj{"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata(d, name), "spec": Obj{"backoffLimit": 0, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 86400, "template": template}}
}
