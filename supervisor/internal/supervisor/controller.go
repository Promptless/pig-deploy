// Package supervisor coordinates restartable releases. Kubernetes status is the
// durable transaction record; every reconcile performs at most one phase change.
package supervisor

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"k8s.io/apimachinery/pkg/util/version"
	"sigs.k8s.io/yaml"
)

var capabilities = []string{"alembic-migrations-v1", "storage-readiness-v1", "native-storage-v1", "migration-ledger-v1", "installation-identity-v1", "controller-self-update-v1", "crd-update-v1"}
var phases = []string{"preflight", "quiesce", "migration", "rollout", "verify", "supervisor", "complete"}
var deploymentName = regexp.MustCompile(`^[a-z]([-a-z0-9]*[a-z0-9])?$`)

const crdPath = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/pigdeployments.governance.promptless.ai"

type Kubernetes interface {
	Get(context.Context, string, string, string) (Obj, error)
	List(context.Context, string, string, string) ([]Obj, error)
	Apply(context.Context, Obj) (Obj, error)
	Patch(context.Context, string, string, string, Obj) (Obj, error)
	Status(context.Context, Obj, Obj) error
	Request(context.Context, string, string, Obj) (Obj, error)
}
type Blocked struct{ Reason, Message string }

func (e *Blocked) Error() string            { return e.Message }
func block(reason, message string) error    { return &Blocked{reason, message} }
func timestamp(t time.Time) string          { return t.Format(time.RFC3339Nano) }
func parseTime(s string) (time.Time, error) { return time.Parse(time.RFC3339Nano, s) }
func Condition(status Obj, name string, truth bool, reason, message string, generation int, now time.Time) {
	state := "False"
	if truth {
		state = "True"
	}
	value := Obj{"type": name, "status": state, "reason": reason, "message": message, "observedGeneration": generation, "lastTransitionTime": timestamp(now)}
	conditions := []any{}
	for _, v := range items(status["conditions"]) {
		entry := object(v)
		if str(entry, "type") == name {
			if str(entry, "status") == state && str(entry, "reason") == reason {
				value["lastTransitionTime"] = entry["lastTransitionTime"]
			}
		} else {
			conditions = append(conditions, entry)
		}
	}
	status["conditions"] = append(conditions, value)
}
func acceptanceCondition(status Obj, digest, hash string, generation int, now time.Time) {
	accepted := str(status, "acceptedConfigurationHash") == hash && str(status, "acceptedReleaseDigest") == digest
	reason, message := "AwaitingCanonicalAcceptance", "Enroll a host, ingest a real trace, and wait for analysis and Dashboard synchronization."
	if accepted {
		reason = "CanonicalSuccess"
		message = "An enrolled host has a readable canonical object, succeeded analysis, and hosted acknowledgement."
	}
	Condition(status, "Acceptance", accepted, reason, message, generation, now)
}
func HelmReleaseName(release Obj) string {
	spec := child(release, "spec")
	name := str(spec, "releaseName")
	if name == "" {
		name = str(child(release, "metadata"), "name")
		if target := str(spec, "targetNamespace"); target != "" {
			name = target + "-" + name
		}
	}
	if len(name) > 53 {
		sum := sha256.Sum256([]byte(name))
		name = name[:40] + "-" + fmt.Sprintf("%x", sum)[:12]
	}
	return name
}
func RecoveryCheck(uid string, selected SelectedRelease, document Obj, now time.Time) error {
	hours := selected.Release.Requirements.RecoveryMaxAgeHours
	if hours == nil {
		return nil
	}
	if document == nil {
		return block("RecoveryConfirmationRequired", "Supply the recovery ConfigMap declared in spec.release.confirmation.")
	}
	data := child(document, "data")
	confirmed, err := parseTime(str(data, "confirmedAt"))
	if err != nil {
		return block("RecoveryConfirmationInvalid", "confirmedAt must be a timestamp with a timezone.")
	}
	if confirmed.After(now) || confirmed.Before(now.Add(-time.Duration(*hours)*time.Hour)) || str(data, "releaseDigest") != "sha256:"+selected.Digest || str(data, "deploymentUID") != uid || str(data, "postgresRecoveryPoint") == "" || str(data, "objectRecoveryPoint") == "" {
		return block("RecoveryConfirmationInvalid", "Refresh the recovery points and bind the confirmation to this deployment and target digest.")
	}
	return nil
}
func CapacityCheck(uid string, selected SelectedRelease, document Obj, now time.Time) error {
	req := selected.Release.Requirements
	if len(req.OperatorCapacityRequirements) == 0 {
		return nil
	}
	data := child(document, "data")
	confirmed, err := parseTime(str(data, "capacityConfirmedAt"))
	if err != nil {
		return block("CapacityConfirmationRequired", "Review the target release operatorCapacityRequirements, make required Terraform changes, then provide a release-specific capacity acknowledgement.")
	}
	if confirmed.After(now) || confirmed.Before(now.Add(-time.Duration(req.CapacityConfirmationMaxAgeHours)*time.Hour)) || str(data, "releaseDigest") != "sha256:"+selected.Digest || str(data, "deploymentUID") != uid || str(data, "capacityRequirementsDigest") != "sha256:"+CanonicalDigest(req.OperatorCapacityRequirements) || str(data, "capacityEvidence") == "" {
		return block("CapacityConfirmationInvalid", "Refresh capacityEvidence and capacityConfirmedAt for this deployment, releaseDigest, and capacityRequirementsDigest. This is operator acknowledgement, not live capacity verification.")
	}
	return nil
}

type Controller struct {
	Kube                                       Kubernetes
	Catalog                                    *Catalog
	Namespace, SystemNamespace, SupervisorName string
	NewTransitionID                            func() string
}

func (c *Controller) transitionID() string {
	if c.NewTransitionID != nil {
		return c.NewTransitionID()
	}
	return uuid.NewString()
}
func (c *Controller) Reconcile(ctx context.Context, d Obj, population int, now time.Time) error {
	status := copyObj(child(d, "status"))
	generation := number(child(d, "metadata"), "generation")
	err := c.reconcile(ctx, d, population, now, status)
	if err != nil {
		var blocked *Blocked
		var validation *ValidationError
		switch {
		case errors.As(err, &blocked):
			Condition(status, "Blocked", true, blocked.Reason, blocked.Message, generation, now)
			Condition(status, "Ready", false, blocked.Reason, blocked.Message, generation, now)
			Condition(status, "Updating", str(status, "targetDigest") != "", blocked.Reason, blocked.Message, generation, now)
		case errors.As(err, &validation):
			message := "Invalid deployment or release configuration: " + validation.Error()
			Condition(status, "Blocked", true, "InvalidConfiguration", message, generation, now)
			Condition(status, "Ready", false, "InvalidConfiguration", message, generation, now)
		default:
			return err
		}
	}
	if !reflect.DeepEqual(copyObj(status), copyObj(child(d, "status"))) {
		return c.Kube.Status(ctx, d, status)
	}
	return nil
}
func (c *Controller) reconcile(ctx context.Context, d Obj, population int, now time.Time, status Obj) error {
	meta := child(d, "metadata")
	generation := number(meta, "generation")
	if population != 1 {
		return block("MultipleDeployments", "One PIGDeployment per watched namespace owns this supervisor's release policy.")
	}
	name := str(meta, "name")
	if len(name) > 54 || !deploymentName.MatchString(name) {
		return block("InvalidDeploymentName", "Use at most 54 lowercase letters, digits, or hyphens, starting with a letter and ending with a letter or digit.")
	}
	spec, err := ParseSpec(child(d, "spec"))
	if err != nil {
		return err
	}
	if err = c.checkHandoff(ctx); err != nil {
		return err
	}
	hash, err := c.ConfigurationHash(ctx, spec)
	if err != nil {
		return err
	}
	if spec.Release.Paused && str(status, "targetDigest") != "" && str(status, "targetDigest") != str(status, "currentDigest") {
		if status["currentManifest"] != nil && status["migrationMayHaveRun"] == false {
			status["targetDigest"] = nil
			status["targetVersion"] = nil
			status["phase"] = "complete"
			delete(status, "retryAt")
		} else {
			return block("Paused", "Update paused; resume with spec.release.paused=false to finish the schema transition before rotating configuration.")
		}
	}
	var selected SelectedRelease
	switch {
	case str(status, "targetDigest") == str(status, "currentDigest") && status["currentManifest"] != nil:
		selected.Release, err = ParseRelease(child(status, "currentManifest"))
		selected.Digest = str(status, "currentDigest")
	case str(status, "targetDigest") != "":
		selected, err = c.Catalog.Select(ctx, str(status, "targetVersion"))
		if err == nil && selected.Digest != str(status, "targetDigest") {
			return block("ImmutableReleaseChanged", "The catalog changed an existing release; restore its original digest.")
		}
	case spec.Release.Paused:
		if status["currentManifest"] == nil {
			return block("Paused", "Unpause to install a release.")
		}
		selected.Release, err = ParseRelease(child(status, "currentManifest"))
		selected.Digest = str(status, "currentDigest")
	default:
		selected, err = c.Catalog.Select(ctx, spec.Release.PinnedVersion)
	}
	if err != nil {
		return err
	}
	currentDigest := str(status, "currentDigest")
	if str(status, "targetDigest") != "" && !spec.Release.Paused {
		requested, err := c.Catalog.Select(ctx, spec.Release.PinnedVersion)
		if err != nil {
			return err
		}
		failed := false
		for _, v := range items(status["conditions"]) {
			x := object(v)
			if str(x, "type") == "Blocked" && str(x, "status") == "True" {
				failed = true
			}
		}
		requestedVersion, _ := stableVersion(requested.Release.Version)
		selectedVersion, _ := stableVersion(selected.Release.Version)
		replacement := requested.Digest != selected.Digest && (spec.Release.PinnedVersion != "" || (failed && requestedVersion.GreaterThan(selectedVersion)))
		if replacement {
			if str(status, "phase") == "migration" {
				active := JobResource(d, spec, selected.Release, selected.Digest, str(status, "transitionConfigurationHash"), "migration", number(status, "attempt"), str(status, "transitionID"))
				name := str(child(active, "metadata"), "name")
				job, err := c.Kube.Get(ctx, "Job", c.Namespace, name)
				if err != nil {
					return err
				}
				running := job != nil && JobOutcome(job) == ""
				if !running {
					running, err = c.migrationPodsRunning(ctx, name)
					if err != nil {
						return err
					}
				}
				if running {
					return block("WaitingForMigration", "The current migration must finish before selecting the requested repair release.")
				}
			}
			if status["migrationMayHaveRun"] != false && requestedVersion.LessThan(selectedVersion) && (str(status, "migrationDigest") != selected.Digest || !contains(selected.Release.RollbackTo, requested.Digest) || requested.Release.Requirements.SchemaTo != selected.Release.Requirements.SchemaTo) {
				return block("RollbackUnsupported", "The in-progress release may have changed the schema. Use its declared compatible rollback, or finish a forward repair before downgrading.")
			}
			selected = requested
			if _, ok := status["migrationMayHaveRun"]; !ok {
				status["migrationMayHaveRun"] = true
			}
			status["targetDigest"] = nil
			status["targetVersion"] = nil
			status["phase"] = "preflight"
		}
	}
	acceptanceCondition(status, selected.Digest, hash, generation, now)
	if selected.Digest == currentDigest && str(status, "targetDigest") == "" && str(status, "phase") == "complete" && str(status, "configurationHash") == hash {
		for _, r := range AnalyzerResources(d, spec, selected.Release, hash) {
			if _, err = c.Kube.Apply(ctx, r); err != nil {
				return err
			}
		}
		ready, err := c.analyzerReady(ctx, d, selected.Release, hash)
		if err != nil {
			return err
		}
		if !ready {
			return block("AnalyzerNotReady", "Waiting for analyzer rollout or Secret rotation.")
		}
		status["configurationHash"] = hash
		if err = c.acceptance(ctx, d, spec, selected, hash, status, now); err != nil {
			return err
		}
		conditions(status, generation, now, true, false)
	} else if str(status, "targetDigest") == "" {
		if err = c.requirements(ctx, spec, selected); err != nil {
			return err
		}
		var recovery Obj
		if spec.Release.Confirmation != nil {
			recovery, err = c.Kube.Get(ctx, "ConfigMap", c.Namespace, spec.Release.Confirmation.ConfigMapRef)
			if err != nil {
				return err
			}
		}
		if selected.Digest != currentDigest {
			if err = RecoveryCheck(str(meta, "uid"), selected, recovery, now); err != nil {
				return err
			}
			if err = CapacityCheck(str(meta, "uid"), selected, recovery, now); err != nil {
				return err
			}
			if len(selected.Release.Requirements.OperatorCapacityRequirements) > 0 {
				Condition(status, "CapacityAcknowledged", true, "OperatorConfirmed", "The operator acknowledged this release's capacity prerequisites; this is not live free-capacity verification.", generation, now)
			}
		}
		if currentDigest != "" {
			currentVersion, err := stableVersion(str(status, "currentVersion"))
			if err != nil {
				return err
			}
			targetVersion, _ := stableVersion(selected.Release.Version)
			if targetVersion.LessThan(currentVersion) {
				current, err := ParseRelease(child(status, "currentManifest"))
				if err != nil {
					return err
				}
				if !contains(current.RollbackTo, selected.Digest) || selected.Release.Requirements.SchemaTo != current.Requirements.SchemaTo {
					return block("RollbackUnsupported", "This schema requires a compatible forward repair release.")
				}
			}
		}
		status["targetDigest"] = selected.Digest
		status["targetVersion"] = selected.Release.Version
		status["phase"] = "preflight"
		status["attempt"] = 0
		status["transitionConfigurationHash"] = hash
		status["transitionID"] = c.transitionID()
		if _, ok := status["migrationMayHaveRun"]; !ok {
			status["migrationMayHaveRun"] = false
		}
		delete(status, "retryAt")
		conditions(status, generation, now, false, true)
	} else {
		phase := str(status, "phase")
		if hash != str(status, "transitionConfigurationHash") {
			if phase == "migration" {
				done, err := c.job(ctx, d, spec, selected, str(status, "transitionConfigurationHash"), phase, status, now, false)
				if err != nil {
					return err
				}
				if !done {
					return block("WaitingForMigration", "The active migration must finish before new configuration is checked.")
				}
			}
			status["phase"] = "preflight"
			status["attempt"] = 0
			status["transitionConfigurationHash"] = hash
			status["transitionID"] = c.transitionID()
			delete(status, "retryAt")
		} else if err = c.Advance(ctx, d, spec, selected, hash, status, now); err != nil {
			return err
		}
		complete := str(status, "phase") == "complete"
		conditions(status, generation, now, complete, !complete)
	}
	status["observedGeneration"] = generation
	return nil
}
func conditions(status Obj, generation int, now time.Time, ready, updating bool) {
	reason, message := "TransitionInProgress", "Release transition is in progress."
	if ready {
		reason = "ReleaseVerified"
		message = "Release installed and dependency checks passed; see Acceptance for end-to-end evidence."
	}
	Condition(status, "Ready", ready, reason, message, generation, now)
	reason, message = "Idle", "No release transition is active."
	if updating {
		reason = "TransitionInProgress"
		message = "A release transition is active."
	}
	Condition(status, "Updating", updating, reason, message, generation, now)
	Condition(status, "Blocked", false, "ChecksPassed", "No customer action is required by the current checks.", generation, now)
}
func (c *Controller) checkHandoff(ctx context.Context) error {
	releases, err := c.Kube.List(ctx, "HelmRelease", c.SystemNamespace, "")
	if err != nil {
		if statusCode(err) == 404 {
			return nil
		}
		return err
	}
	for _, release := range releases {
		spec := child(release, "spec")
		target := str(spec, "targetNamespace")
		if target == "" {
			target = str(child(release, "metadata"), "namespace")
			if target == "" {
				target = c.SystemNamespace
			}
		}
		if target == c.SystemNamespace && HelmReleaseName(release) == c.SupervisorName && !boolean(spec, "suspend") {
			return block("FluxHandoffRequired", "Suspend the bootstrap HelmRelease before PIG manages releases.")
		}
	}
	return nil
}
func (c *Controller) ConfigurationHash(ctx context.Context, spec DeploymentSpec) (string, error) {
	account, err := c.Kube.Get(ctx, "ServiceAccount", c.Namespace, spec.ServiceAccountName)
	if err != nil {
		return "", err
	}
	if account == nil {
		return "", block("ServiceAccountMissing", "Deliver the customer-owned analyzer ServiceAccount before installation.")
	}
	revisions := Obj{"serviceAccount/" + spec.ServiceAccountName: child(account, "metadata")["resourceVersion"]}
	for _, ref := range SecretRefs(spec) {
		secret, err := c.Kube.Get(ctx, "Secret", c.Namespace, ref.Name)
		if err != nil {
			return "", err
		}
		if secret == nil || str(child(secret, "data"), ref.Key) == "" {
			return "", block("SecretKeyMissing", fmt.Sprintf("Deliver key %s in Secret %s.", ref.Key, ref.Name))
		}
		revisions[ref.Name] = child(secret, "metadata")["resourceVersion"]
	}
	if ca := spec.Storage.Postgres.CAConfigMapRef; ca != nil {
		document, err := c.Kube.Get(ctx, "ConfigMap", c.Namespace, ca.Name)
		if err != nil {
			return "", err
		}
		if _, ok := child(document, "data")[ca.Key]; document == nil || !ok {
			return "", block("PostgresCAMissing", "Deliver the PostgreSQL CA ConfigMap before installation.")
		}
		revisions["ca/"+ca.Name] = child(document, "metadata")["resourceVersion"]
	}
	settings := asObj(spec)
	delete(settings, "release")
	return CanonicalDigest([]any{settings, revisions}), nil
}
func (c *Controller) requirements(ctx context.Context, spec DeploymentSpec, selected SelectedRelease) error {
	req := selected.Release.Requirements
	for _, v := range req.Capabilities {
		if !contains(capabilities, v) {
			return block("RBACUpgradeRequired", "This release needs capabilities outside the bootstrap RBAC; review and apply an explicit bootstrap upgrade.")
		}
	}
	document, err := c.Kube.Request(ctx, "GET", "/version", nil)
	if err != nil {
		return err
	}
	v, err := version.ParseSemantic(strings.Split(strings.TrimPrefix(str(document, "gitVersion"), "v"), "-")[0])
	if err != nil {
		return invalid("kubernetes.gitVersion")
	}
	minimum, err := version.ParseSemantic(req.KubernetesMin)
	if err != nil {
		return invalid("requirements.kubernetesMin")
	}
	if v.LessThan(minimum) {
		return block("KubernetesUpgradeRequired", "Upgrade the existing cluster through customer infrastructure management.")
	}
	backend := "gcs"
	if spec.Storage.S3 != nil {
		backend = "s3"
	} else if spec.Storage.AzureBlob != nil {
		backend = "azureBlob"
	}
	if !contains(req.StorageBackends, backend) {
		return block("StorageBackendUnsupported", "The target release does not support the configured native backend.")
	}
	return nil
}
func (c *Controller) Advance(ctx context.Context, d Obj, spec DeploymentSpec, selected SelectedRelease, hash string, status Obj, now time.Time) error {
	phase := str(status, "phase")
	switch phase {
	case "preflight", "migration", "verify":
		if phase == "migration" {
			resource := JobResource(d, spec, selected.Release, selected.Digest, hash, phase, number(status, "attempt"), str(status, "transitionID"))
			job, err := c.Kube.Get(ctx, "Job", c.Namespace, str(child(resource, "metadata"), "name"))
			if err != nil {
				return err
			}
			if job == nil {
				var recovery Obj
				if spec.Release.Confirmation != nil {
					recovery, err = c.Kube.Get(ctx, "ConfigMap", c.Namespace, spec.Release.Confirmation.ConfigMapRef)
					if err != nil {
						return err
					}
				}
				uid := str(child(d, "metadata"), "uid")
				if err = RecoveryCheck(uid, selected, recovery, now); err != nil {
					return err
				}
				if err = CapacityCheck(uid, selected, recovery, now); err != nil {
					return err
				}
			}
		}
		done, err := c.job(ctx, d, spec, selected, hash, phase, status, now, true)
		if err != nil || !done {
			return err
		}
	case "quiesce":
		name := str(child(d, "metadata"), "name") + "-analyzer"
		resource, err := c.Kube.Get(ctx, "Deployment", c.Namespace, name)
		if err != nil {
			return err
		}
		if resource != nil {
			owned := false
			for _, v := range items(child(resource, "metadata")["ownerReferences"]) {
				if str(object(v), "uid") == str(child(d, "metadata"), "uid") {
					owned = true
				}
			}
			if !owned {
				return block("OwnershipConflict", "The analyzer Deployment is not owned by this PIGDeployment.")
			}
			quiesced := AnalyzerResources(d, spec, selected.Release, hash)[0]
			child(quiesced, "spec")["replicas"] = 0
			resource, err = c.Kube.Apply(ctx, quiesced)
			if err != nil {
				return err
			}
			if number(child(resource, "status"), "observedGeneration") < number(child(resource, "metadata"), "generation") || number(child(resource, "status"), "replicas") != 0 {
				return nil
			}
		}
		pods, err := c.Kube.List(ctx, "Pod", c.Namespace, "app.kubernetes.io/name="+name+",app.kubernetes.io/component=analyzer")
		if err != nil {
			return err
		}
		if podsRunning(pods) {
			return nil
		}
	case "rollout":
		for _, r := range AnalyzerResources(d, spec, selected.Release, hash) {
			if _, err := c.Kube.Apply(ctx, r); err != nil {
				return err
			}
		}
		ready, err := c.analyzerReady(ctx, d, selected.Release, hash)
		if err != nil || !ready {
			return err
		}
	case "supervisor":
		if selected.Digest != str(status, "currentDigest") {
			if err := c.selfUpdate(ctx, selected); err != nil {
				return err
			}
		}
		resource, err := c.Kube.Get(ctx, "Deployment", c.SystemNamespace, c.SupervisorName)
		if err != nil {
			return err
		}
		if resource == nil {
			return nil
		}
		ready, err := DeploymentReady(resource, selected.Release.SupervisorImage)
		if err != nil || !ready {
			return err
		}
	case "complete":
		status["currentDigest"] = selected.Digest
		status["currentVersion"] = selected.Release.Version
		status["currentManifest"] = asObj(selected.Release)
		status["configurationHash"] = hash
		status["targetDigest"] = nil
		status["targetVersion"] = nil
		status["attempt"] = 0
		status["migrationMayHaveRun"] = false
		delete(status, "migrationDigest")
		return nil
	default:
		return invalid("status.phase")
	}
	next := ""
	for i, p := range phases {
		if p == phase && i+1 < len(phases) {
			next = phases[i+1]
		}
	}
	if phase == "quiesce" && selected.Digest == str(status, "currentDigest") {
		next = "rollout"
	}
	status["phase"] = next
	status["attempt"] = 0
	if next == "migration" {
		if status["migrationMayHaveRun"] == false {
			status["migrationDigest"] = selected.Digest
		}
		status["migrationMayHaveRun"] = true
	}
	return nil
}
func JobOutcome(job Obj) string {
	for _, v := range items(child(job, "status")["conditions"]) {
		entry := object(v)
		if contains([]string{"Complete", "Failed"}, str(entry, "type")) && str(entry, "status") == "True" {
			return str(entry, "type")
		}
	}
	return ""
}
func (c *Controller) job(ctx context.Context, d Obj, spec DeploymentSpec, selected SelectedRelease, hash, phase string, status Obj, now time.Time, start bool) (bool, error) {
	resource := JobResource(d, spec, selected.Release, selected.Digest, hash, phase, number(status, "attempt"), str(status, "transitionID"))
	name := str(child(resource, "metadata"), "name")
	job, err := c.Kube.Get(ctx, "Job", c.Namespace, name)
	if err != nil {
		return false, err
	}
	if phase == "migration" {
		running, err := c.migrationPodsRunning(ctx, name)
		if err != nil || running {
			return false, err
		}
	}
	if job == nil {
		if start {
			_, err = c.Kube.Apply(ctx, resource)
		}
		return !start, err
	}
	switch JobOutcome(job) {
	case "Complete":
		return true, nil
	case "Failed":
		if !start {
			return true, nil
		}
		if str(status, "retryAt") == "" {
			status["retryAt"] = timestamp(now.Add(2 * time.Minute))
		} else {
			retry, err := parseTime(str(status, "retryAt"))
			if err != nil {
				return false, invalid("status.retryAt")
			}
			if !now.Before(retry) {
				status["attempt"] = number(status, "attempt") + 1
				delete(status, "retryAt")
			}
		}
		reason := "VerificationBlocked"
		if phase == "preflight" {
			reason = "DependencyCheckFailed"
		} else if phase == "migration" {
			reason = "MigrationBlocked"
		}
		return false, block(reason, fmt.Sprintf("Inspect Job %s; correct infrastructure through Terraform or deliver the required configuration. Checks retry automatically.", name))
	}
	return false, nil
}
func podsRunning(pods []Obj) bool {
	for _, p := range pods {
		phase := str(child(p, "status"), "phase")
		if phase != "Succeeded" && phase != "Failed" {
			return true
		}
	}
	return false
}
func (c *Controller) migrationPodsRunning(ctx context.Context, name string) (bool, error) {
	pods, err := c.Kube.List(ctx, "Pod", c.Namespace, "governance.promptless.ai/job="+name)
	return podsRunning(pods), err
}
func DeploymentReady(resource Obj, image string) (bool, error) {
	state := child(resource, "status")
	containers := items(child(resource, "spec", "template", "spec")["containers"])
	generation := number(child(resource, "metadata"), "generation")
	if generation == 0 {
		generation = 1
	}
	if len(containers) == 0 || str(object(containers[0]), "image") != image || number(state, "observedGeneration") < generation {
		return false, nil
	}
	for _, v := range items(state["conditions"]) {
		e := object(v)
		if str(e, "type") == "Progressing" && str(e, "status") == "False" && str(e, "reason") == "ProgressDeadlineExceeded" {
			return false, block("RolloutFailed", "A Deployment exceeded its progress deadline. Inspect its Pods and correct the failure or select a compatible repair release.")
		}
	}
	return number(state, "updatedReplicas") == 1 && number(state, "availableReplicas") == 1 && number(state, "replicas") == 1, nil
}
func (c *Controller) analyzerReady(ctx context.Context, d Obj, r Release, hash string) (bool, error) {
	resource, err := c.Kube.Get(ctx, "Deployment", c.Namespace, str(child(d, "metadata"), "name")+"-analyzer")
	if err != nil || resource == nil {
		return false, err
	}
	ready, err := DeploymentReady(resource, r.AnalyzerImage)
	return ready && str(child(resource, "spec", "template", "metadata", "annotations"), "governance.promptless.ai/config-hash") == hash, err
}
func (c *Controller) selfUpdate(ctx context.Context, selected SelectedRelease) error {
	if selected.Release.CRD != nil {
		data, err := c.Catalog.VerifiedBytes(ctx, *selected.Release.CRD)
		if err != nil {
			return err
		}
		var crd Obj
		if err = yaml.UnmarshalStrict(data, &crd); err != nil {
			return invalid("release.crd")
		}
		conversion := str(child(crd, "spec", "conversion"), "strategy")
		if str(crd, "kind") != "CustomResourceDefinition" || str(child(crd, "metadata"), "name") != "pigdeployments.governance.promptless.ai" || str(child(crd, "spec"), "scope") != "Namespaced" || (conversion != "" && conversion != "None") {
			return block("CRDUpgradeUnsupported", "The release CRD exceeds the granted bootstrap contract.")
		}
		installed, err := c.Kube.Request(ctx, "GET", crdPath, nil)
		if err != nil {
			return err
		}
		merged := MergeCRD(child(installed, "spec"), child(crd, "spec"))
		if merged == nil {
			return block("CRDUpgradeUnsupported", "The shared CRD change is not additive. Review an explicit bootstrap upgrade across every watched namespace.")
		}
		if !reflect.DeepEqual(copyObj(merged), copyObj(child(installed, "spec"))) {
			if _, err = c.Kube.Request(ctx, "PATCH", crdPath, Obj{"metadata": Obj{"resourceVersion": child(installed, "metadata")["resourceVersion"]}, "spec": merged}); err != nil {
				return err
			}
		}
	}
	_, err := c.Kube.Patch(ctx, "Deployment", c.SystemNamespace, c.SupervisorName, Obj{"spec": Obj{"template": Obj{"spec": Obj{"containers": []any{Obj{"name": "supervisor", "image": selected.Release.SupervisorImage}}}}}})
	return err
}
func (c *Controller) acceptance(ctx context.Context, d Obj, spec DeploymentSpec, selected SelectedRelease, hash string, status Obj, now time.Time) error {
	attempt := number(status, "acceptanceAttempt")
	document := JobResource(d, spec, selected.Release, selected.Digest, hash, "acceptance", attempt, "")
	name := str(child(document, "metadata"), "name")
	job, err := c.Kube.Get(ctx, "Job", c.Namespace, name)
	if err != nil {
		return err
	}
	if job == nil {
		if _, err = c.Kube.Apply(ctx, document); err != nil {
			return err
		}
	}
	outcome := JobOutcome(job)
	accepted := outcome == "Complete"
	if outcome != "" {
		next := str(status, "acceptanceNextCheck")
		if next == "" {
			delay := 2 * time.Minute
			if accepted {
				delay = time.Hour
			}
			status["acceptanceNextCheck"] = timestamp(now.Add(delay))
		} else {
			check, err := parseTime(next)
			if err != nil {
				return invalid("status.acceptanceNextCheck")
			}
			if !now.Before(check) {
				status["acceptanceAttempt"] = attempt + 1
				delete(status, "acceptanceNextCheck")
			}
		}
	}
	if accepted {
		status["acceptedConfigurationHash"] = hash
		status["acceptedReleaseDigest"] = selected.Digest
		if str(status, "acceptedJob") != name {
			completed := str(child(job, "status"), "completionTime")
			if completed == "" {
				completed = timestamp(now)
			}
			status["lastAcceptanceAt"] = completed
			status["acceptedJob"] = name
		}
	}
	acceptanceCondition(status, selected.Digest, hash, number(child(d, "metadata"), "generation"), now)
	return nil
}
