package supervisor

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"strings"

	"github.com/Promptless/pig-deploy/schemas"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"k8s.io/apimachinery/pkg/util/version"
)

type SecretRef struct {
	Name string `json:"name"`
	Key  string `json:"key"`
}
type Confirmation struct {
	ConfigMapRef string `json:"configMapRef"`
}
type ReleasePolicy struct {
	Channel       string        `json:"channel"`
	Paused        bool          `json:"paused"`
	PinnedVersion string        `json:"pinnedVersion"`
	Confirmation  *Confirmation `json:"confirmation"`
}
type Hosted struct {
	RuntimeURL            string    `json:"runtimeURL"`
	InstallTokenSecretRef SecretRef `json:"installTokenSecretRef"`
}
type Endpoint struct {
	Enabled            bool              `json:"enabled"`
	Hostname           string            `json:"hostname"`
	IngressClassName   string            `json:"ingressClassName"`
	TLSSecretName      *string           `json:"tlsSecretName"`
	IngressAnnotations map[string]string `json:"ingressAnnotations"`
}
type Postgres struct {
	DSNSecretRef          SecretRef  `json:"dsnSecretRef"`
	MigrationDSNSecretRef *SecretRef `json:"migrationDsnSecretRef"`
	CAConfigMapRef        *SecretRef `json:"caConfigMapRef"`
}
type S3 struct {
	Region string `json:"region"`
	Bucket string `json:"bucket"`
	Prefix string `json:"prefix"`
}
type AzureBlob struct {
	AccountURL string `json:"accountURL"`
	Container  string `json:"container"`
	Prefix     string `json:"prefix"`
}
type GCS struct {
	Bucket string `json:"bucket"`
	Prefix string `json:"prefix"`
}
type Storage struct {
	Postgres  Postgres   `json:"postgres"`
	S3        *S3        `json:"s3"`
	AzureBlob *AzureBlob `json:"azureBlob"`
	GCS       *GCS       `json:"gcs"`
}
type Model struct {
	Provider        string     `json:"provider"`
	Authentication  string     `json:"authentication"`
	BaseURL         string     `json:"baseURL"`
	Name            string     `json:"name"`
	APIKeySecretRef *SecretRef `json:"apiKeySecretRef"`
}
type Analysis struct {
	ActivationAt     string  `json:"activationAt"`
	QuietWindowHours float64 `json:"quietWindowHours"`
	Model            Model   `json:"model"`
}
type DeploymentSpec struct {
	Release            ReleasePolicy     `json:"release"`
	ServiceAccountName string            `json:"serviceAccountName"`
	PodLabels          map[string]string `json:"podLabels"`
	NodeSelector       map[string]string `json:"nodeSelector"`
	Hosted             Hosted            `json:"hosted"`
	Endpoint           Endpoint          `json:"endpoint"`
	Storage            Storage           `json:"storage"`
	Analysis           Analysis          `json:"analysis"`
}
type Requirements struct {
	KubernetesMin                   string   `json:"kubernetesMin"`
	PostgresMin                     int      `json:"postgresMin"`
	PostgresMax                     int      `json:"postgresMax"`
	OperatorCapacityRequirements    []string `json:"operatorCapacityRequirements"`
	CapacityConfirmationMaxAgeHours int      `json:"capacityConfirmationMaxAgeHours"`
	StorageBackends                 []string `json:"storageBackends"`
	Capabilities                    []string `json:"capabilities"`
	RecoveryMaxAgeHours             *int     `json:"recoveryMaxAgeHours"`
	DestructiveMigration            bool     `json:"destructiveMigration"`
	SchemaFrom                      []int    `json:"schemaFrom"`
	SchemaTo                        int      `json:"schemaTo"`
	ControllerProtocol              int      `json:"controllerProtocol"`
}
type Artifact struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
}
type ChartArtifact struct {
	Repository string `json:"repository"`
	Version    string `json:"version"`
	SHA256     string `json:"sha256"`
	OCIDigest  string `json:"ociDigest"`
}
type ReleaseArtifacts struct {
	SourceCommit           string        `json:"sourceCommit"`
	TerraformArchiveURL    string        `json:"terraformArchiveURL"`
	TerraformArchiveSHA256 string        `json:"terraformArchiveSha256"`
	SupervisorChart        ChartArtifact `json:"supervisorChart"`
	WorkerChart            ChartArtifact `json:"workerChart"`
}
type Release struct {
	Version         string            `json:"version"`
	AnalyzerImage   string            `json:"analyzerImage"`
	SupervisorImage string            `json:"supervisorImage"`
	Requirements    Requirements      `json:"requirements"`
	RollbackTo      []string          `json:"rollbackTo"`
	CRD             *Artifact         `json:"crd"`
	Artifacts       *ReleaseArtifacts `json:"artifacts"`
}
type SelectedRelease struct {
	Release Release
	Digest  string
}

type ValidationError struct{ Field string }

func (e *ValidationError) Error() string { return "invalid configuration at " + e.Field }
func invalid(field string) error         { return &ValidationError{field} }

// Schema validation preserves the public contract, including rejection of unknown
// fields. Do not include validation error text: it can contain credential inputs.
func compileSchema(name string, data []byte) *jsonschema.Schema {
	var document any
	if err := json.Unmarshal(data, &document); err != nil {
		panic(err)
	}
	c := jsonschema.NewCompiler()
	if err := c.AddResource(name, document); err != nil {
		panic(err)
	}
	s, err := c.Compile(name)
	if err != nil {
		panic(err)
	}
	return s
}

var deploymentSchema = compileSchema("deployment.json", schemas.Deployment)
var releaseSchema = compileSchema("release.json", schemas.Release)

func validate(schema *jsonschema.Schema, value any) error {
	if err := schema.Validate(value); err != nil {
		field := "<root>"
		if e, ok := err.(*jsonschema.ValidationError); ok {
			for len(e.Causes) > 0 {
				e = e.Causes[0]
			}
			if len(e.InstanceLocation) > 0 {
				field = strings.Join(e.InstanceLocation, ".")
			}
		}
		return invalid(field)
	}
	return nil
}

func ParseSpec(value Obj) (DeploymentSpec, error) {
	s := DeploymentSpec{Release: ReleasePolicy{Channel: "stable"}, PodLabels: map[string]string{}, NodeSelector: map[string]string{}, Hosted: Hosted{RuntimeURL: "https://api.gopromptless.ai"}, Endpoint: Endpoint{IngressAnnotations: map[string]string{}}, Analysis: Analysis{QuietWindowHours: 0.5}}
	if err := validate(deploymentSchema, value); err != nil {
		return s, err
	}
	if err := decode(value, &s); err != nil {
		return s, invalid("spec")
	}
	if s.Endpoint.Enabled && (s.Endpoint.Hostname == "" || strings.TrimSpace(s.Endpoint.IngressClassName) == "") {
		return s, invalid("endpoint")
	}
	if s.Release.PinnedVersion != "" {
		if _, err := stableVersion(s.Release.PinnedVersion); err != nil {
			return s, invalid("release.pinnedVersion")
		}
	}
	u, err := url.Parse(s.Hosted.RuntimeURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return s, invalid("hosted.runtimeURL")
	}
	s.Hosted.RuntimeURL = strings.TrimRight(s.Hosted.RuntimeURL, "/")
	backends := 0
	if s.Storage.S3 != nil {
		backends++
	}
	if s.Storage.AzureBlob != nil {
		backends++
	}
	if s.Storage.GCS != nil {
		backends++
	}
	if backends != 1 {
		return s, invalid("storage")
	}
	m := s.Analysis.Model
	if (m.Authentication == "aws_sigv4" && (m.Provider != "aws_bedrock" || m.APIKeySecretRef != nil)) || (m.Authentication == "api_key" && m.APIKeySecretRef == nil) {
		return s, invalid("analysis.model")
	}
	u, err = url.Parse(strings.TrimRight(m.BaseURL, "/"))
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" || u.RawQuery != "" || u.Fragment != "" {
		return s, invalid("analysis.model.baseURL")
	}
	host := strings.ToLower(u.Hostname())
	allowed := (m.Provider == "openai" && host == "api.openai.com" && u.Path == "/v1") ||
		(m.Provider == "azure_openai" && (strings.HasSuffix(host, ".openai.azure.com") || strings.HasSuffix(host, ".services.ai.azure.com")) && u.Path == "/openai/v1") ||
		(m.Provider == "aws_bedrock" && bedrockHost.MatchString(host) && (u.Path == "/v1" || u.Path == "/openai/v1"))
	if !allowed {
		return s, invalid("analysis.model.baseURL")
	}
	return s, nil
}

var bedrockHost = regexp.MustCompile(`^bedrock-mantle\.[a-z0-9-]+\.api\.aws$`)
var stablePattern = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

func stableVersion(v string) (*version.Version, error) {
	if !stablePattern.MatchString(v) {
		return nil, invalid("version")
	}
	return version.ParseSemantic(v)
}
func ParseRelease(value Obj) (Release, error) {
	r := Release{RollbackTo: []string{}, Requirements: Requirements{KubernetesMin: "1.30.0", PostgresMin: 15, PostgresMax: 18, OperatorCapacityRequirements: []string{}, CapacityConfirmationMaxAgeHours: 24, Capabilities: []string{}, ControllerProtocol: 1}}
	if err := validate(releaseSchema, value); err != nil {
		return r, err
	}
	if err := decode(value, &r); err != nil {
		return r, invalid("release")
	}
	if _, err := stableVersion(r.Version); err != nil {
		return r, err
	}
	if _, err := version.ParseSemantic(r.Requirements.KubernetesMin); err != nil {
		return r, invalid("requirements.kubernetesMin")
	}
	if r.Requirements.DestructiveMigration && r.Requirements.RecoveryMaxAgeHours == nil {
		return r, invalid("requirements.recoveryMaxAgeHours")
	}
	return r, nil
}
func decode(value any, target any) error {
	b, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, target)
}
func strictJSON(data []byte, target any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		return fmt.Errorf("invalid JSON document")
	}
	if d.Decode(new(any)) != io.EOF {
		return fmt.Errorf("invalid trailing JSON")
	}
	return nil
}
