package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"reflect"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
)

func exampleDeployment(t *testing.T) Obj {
	t.Helper()
	data, err := os.ReadFile("../../../examples/pig-deployment.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var d Obj
	if err = yaml.Unmarshal(data, &d); err != nil {
		t.Fatal(err)
	}
	d["metadata"] = Obj{"name": "acme", "namespace": "pig", "uid": "installation-uid", "generation": 1, "resourceVersion": "1"}
	return d
}
func releaseDocument(v string) Obj {
	return Obj{"version": v, "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + strings.Repeat("a", 64), "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + strings.Repeat("b", 64), "requirements": Obj{"storageBackends": []any{"s3", "azureBlob", "gcs"}, "schemaFrom": []any{0, 1}, "schemaTo": 1}}
}
func TestWorkloadCredentialAndSchedulingBoundaries(t *testing.T) {
	for _, auth := range []string{"api_key", "aws_sigv4"} {
		t.Run(auth, func(t *testing.T) {
			d := exampleDeployment(t)
			s := child(d, "spec")
			s["nodeSelector"] = Obj{"iam.gke.io/gke-metadata-server-enabled": "true"}
			child(s, "hosted")["runtimeURL"] = "https://staging.example.com/"
			child(s, "storage", "postgres")["migrationDsnSecretRef"] = Obj{"name": "migration-only", "key": "dsn"}
			if auth == "aws_sigv4" {
				child(s, "analysis")["model"] = Obj{"provider": "aws_bedrock", "authentication": auth, "baseURL": "https://bedrock-mantle.us-east-1.api.aws/v1", "name": "test-model"}
			}
			spec, err := ParseSpec(s)
			if err != nil {
				t.Fatal(err)
			}
			doc := releaseDocument("1.0.0")
			child(doc, "requirements")["capabilities"] = []any{"storage-readiness-v1"}
			release, err := ParseRelease(doc)
			if err != nil {
				t.Fatal(err)
			}
			workload := AnalyzerResources(d, spec, release, "hash")[0]
			phases := []string{"serve", "preflight", "migration", "verify", "acceptance"}
			for _, phase := range phases {
				if phase != "serve" {
					workload = JobResource(d, spec, release, "digest", "hash", phase, 0, "")
				}
				template := child(workload, "spec", "template")
				container := object(items(child(template, "spec")["containers"])[0])
				env := map[string]Obj{}
				for _, v := range items(container["env"]) {
					entry := object(v)
					env[str(entry, "name")] = entry
					if strings.HasPrefix(str(entry, "name"), "INSTRUCTION_HUB_ANALYSIS_REPOSITORY_") {
						t.Fatal("repository credential leaked into workload")
					}
				}
				_, migration := env["INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN"]
				if migration != (phase == "preflight" || phase == "migration") {
					t.Fatalf("migration credentials in %s", phase)
				}
				if str(env["INSTRUCTION_HUB_RUNTIME_BASE_URL"], "value") != "https://staging.example.com" || str(child(env["INSTRUCTION_HUB_INSTALL_TOKEN"], "valueFrom", "secretKeyRef"), "key") != "install-token" {
					t.Fatal("hosted identity missing")
				}
				if !reflect.DeepEqual(child(template, "spec", "nodeSelector"), s["nodeSelector"]) {
					t.Fatal("identity scheduling constraint missing")
				}
				if phase == "serve" {
					if str(child(container, "readinessProbe", "httpGet"), "path") != "/readyz" {
						t.Fatal("storage readiness missing")
					}
				} else {
					if container["ports"] != nil || container["startupProbe"] != nil || str(child(template, "metadata", "labels"), "app.kubernetes.io/component") != "maintenance" {
						t.Fatal("maintenance selected by service")
					}
				}
			}
		})
	}
}
func TestSpecValidationAndSecretRedaction(t *testing.T) {
	cases := map[string]func(Obj){
		"unknown":             func(s Obj) { s["unknown"] = "private-value" },
		"name":                func(s Obj) { s["serviceAccountName"] = "private-value!" },
		"two backends":        func(s Obj) { child(s, "storage")["gcs"] = Obj{"bucket": "example", "prefix": "test"} },
		"no backend":          func(s Obj) { delete(child(s, "storage"), "s3") },
		"runtime credentials": func(s Obj) { child(s, "hosted")["runtimeURL"] = "https://private-value@example.com" },
		"model wrong origin":  func(s Obj) { child(s, "analysis", "model")["baseURL"] = "https://private-value.example/v1" },
		"model port":          func(s Obj) { child(s, "analysis", "model")["baseURL"] = "https://api.openai.com:443/v1" },
		"ambient openai":      func(s Obj) { child(s, "analysis", "model")["authentication"] = "aws_sigv4" },
		"missing key":         func(s Obj) { delete(child(s, "analysis", "model"), "apiKeySecretRef") },
		"invalid pin":         func(s Obj) { s["release"] = Obj{"pinnedVersion": "1.0.0-rc1"} },
		"zero quiet window":   func(s Obj) { child(s, "analysis")["quietWindowHours"] = 0 },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			s := child(exampleDeployment(t), "spec")
			change(s)
			_, err := ParseSpec(s)
			if err == nil || strings.Contains(err.Error(), "private-value") {
				t.Fatalf("unsafe validation: %v", err)
			}
		})
	}
}

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func catalogFixture(documents ...Obj) (*Catalog, map[string][]byte) {
	c := NewCatalog("https://catalog.example/stable.json")
	bodies := map[string][]byte{}
	entries := []any{}
	for _, d := range documents {
		data, _ := json.Marshal(d)
		sum := sha256.Sum256(data)
		url := "https://raw.githubusercontent.com/Promptless/pig-deploy/" + strings.Repeat("e", 40) + "/catalog/releases/" + str(d, "version") + ".json"
		bodies[url] = data
		entries = append(entries, Obj{"version": d["version"], "url": url, "sha256": hex.EncodeToString(sum[:])})
	}
	bodies[c.URL], _ = json.Marshal(Obj{"schemaVersion": 1, "releases": entries})
	c.Client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(bodies[r.URL.String()]))), Header: http.Header{}, Request: r}, nil
	})
	return c, bodies
}
func TestCatalogStablePinIntegrityAndBounds(t *testing.T) {
	ctx := context.Background()
	c, bodies := catalogFixture(releaseDocument("1.0.0"), releaseDocument("2.0.0"))
	selected, err := c.Select(ctx, "")
	if err != nil || selected.Release.Version != "2.0.0" {
		t.Fatal(selected, err)
	}
	selected, err = c.Select(ctx, "1.0.0")
	if err != nil || selected.Release.Version != "1.0.0" {
		t.Fatal(selected, err)
	}
	if _, err = c.Select(ctx, "1.1.0"); err == nil {
		t.Fatal("pin silently fell back")
	}
	for address := range bodies {
		if address != c.URL {
			bodies[address] = []byte("tampered")
		}
	}
	if _, err = c.Select(ctx, ""); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatal(err)
	}
	for _, version := range []string{"1.0.0-rc1", "1.0.0+local", "v1.0.0", "01.0.0", "1.0"} {
		c, _ := catalogFixture(releaseDocument(version))
		if _, err = c.Select(ctx, ""); err == nil {
			t.Fatalf("accepted unstable version %s", version)
		}
	}
	c, _ = catalogFixture(releaseDocument("1.0.0"), releaseDocument("1.0.0"))
	if _, err = c.Select(ctx, ""); err == nil {
		t.Fatal("accepted duplicate versions")
	}
	c, bodies = catalogFixture(releaseDocument("1.0.0"))
	bodies[c.URL] = []byte(strings.Repeat(" ", MaxCatalogBytes+1))
	if _, err = c.Select(ctx, ""); err == nil || !strings.Contains(err.Error(), "size limit") {
		t.Fatal(err)
	}
	c.Client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 403, Header: http.Header{}, Body: io.NopCloser(strings.NewReader("private-response"))}, nil
	})
	c.URL = "https://private-user:private-password@example.com/path?token=private-token"
	_, err = c.Select(ctx, "")
	if err == nil || strings.Contains(err.Error(), "private-") {
		t.Fatal(err)
	}
	doc := releaseDocument("1.0.0")
	child(doc, "requirements")["destructiveMigration"] = true
	if _, err = ParseRelease(doc); err == nil {
		t.Fatal("destructive release without recovery accepted")
	}
}
func TestSharedCRDAllowsOnlyOptionalAdditions(t *testing.T) {
	data, err := os.ReadFile("../../../charts/pig-supervisor/crds/pigdeployments.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var crd Obj
	if err = yaml.Unmarshal(data, &crd); err != nil {
		t.Fatal(err)
	}
	base := child(crd, "spec")
	schema := func(s Obj) Obj {
		return child(object(items(s["versions"])[0]), "schema", "openAPIV3Schema", "properties", "spec")
	}
	left, right := copyObj(base), copyObj(base)
	child(schema(left), "properties")["optionalA"] = Obj{"type": "string"}
	child(schema(right), "properties")["optionalB"] = Obj{"type": "string"}
	merged := MergeCRD(left, right)
	if merged == nil || !AdditiveCRD(left, merged) || !AdditiveCRD(right, merged) {
		t.Fatal("optional field union rejected")
	}
	if child(schema(merged), "properties")["optionalA"] == nil || child(schema(merged), "properties")["optionalB"] == nil {
		t.Fatal("independent namespace field removed")
	}
	for _, change := range []func(Obj){
		func(s Obj) { schema(s)["required"] = append(items(schema(s)["required"]), "optionalA") },
		func(s Obj) { child(schema(s), "properties", "serviceAccountName")["maxLength"] = 10 },
		func(s Obj) { s["scope"] = "Cluster" },
		func(s Obj) { s["conversion"] = Obj{"strategy": "Webhook"} },
	} {
		target := copyObj(left)
		change(target)
		if MergeCRD(base, target) != nil {
			t.Fatal("accepted incompatible CRD")
		}
	}
}
