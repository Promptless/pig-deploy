package supervisor

import (
	"context"
	"testing"
	"time"
)

// acceptanceKube serves one acceptance Job and its Pods and records applied Jobs.
type acceptanceKube struct {
	Kubernetes
	job     Obj
	pods    []Obj
	applied []string
}

func (k *acceptanceKube) Get(_ context.Context, kind, _, name string) (Obj, error) {
	if kind == "Job" && k.job != nil && str(child(k.job, "metadata"), "name") == name {
		return k.job, nil
	}
	return nil, nil
}
func (k *acceptanceKube) List(context.Context, string, string, string) ([]Obj, error) {
	return k.pods, nil
}
func (k *acceptanceKube) Apply(_ context.Context, r Obj) (Obj, error) {
	k.applied = append(k.applied, str(child(r, "metadata"), "name"))
	return r, nil
}

func terminatedPod(message string) Obj {
	return Obj{"status": Obj{"containerStatuses": []any{Obj{"state": Obj{"terminated": Obj{"message": message}}}}}}
}

func TestAcceptanceRetryDelayDoublesToHourlyCap(t *testing.T) {
	expected := []time.Duration{2 * time.Minute, 4 * time.Minute, 8 * time.Minute, 16 * time.Minute, 32 * time.Minute, time.Hour, time.Hour}
	for failures, want := range expected {
		if got := AcceptanceRetryDelay(failures); got != want {
			t.Fatalf("failures %d: got %s, want %s", failures, got, want)
		}
	}
	if got := AcceptanceRetryDelay(10_000); got != time.Hour {
		t.Fatalf("large failure count: got %s", got)
	}
}

func TestJobFailureReasonAcceptsOnlyBoundedFailures(t *testing.T) {
	for _, test := range []struct {
		name string
		pods []Obj
		want string
	}{
		{"reported failure", []Obj{terminatedPod(`{"ok": false, "reason": "AnalysisNotAllowed"}`)}, "AnalysisNotAllowed"},
		{"no pods after deadline", nil, ""},
		{"success is not a failure", []Obj{terminatedPod(`{"ok": true, "reason": "Completed"}`)}, ""},
		{"unstructured message", []Obj{terminatedPod("Traceback: secret detail")}, ""},
		{"reason outside condition format", []Obj{terminatedPod(`{"ok": false, "reason": "contains spaces and /paths"}`)}, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := JobFailureReason(test.pods); got != test.want {
				t.Fatalf("got %q, want %q", got, test.want)
			}
		})
	}
}

func TestFailedAcceptanceReportsReasonAndBacksOffUntilAccepted(t *testing.T) {
	d := exampleDeployment(t)
	spec, err := ParseSpec(child(d, "spec"))
	if err != nil {
		t.Fatal(err)
	}
	release, err := ParseRelease(releaseDocument("1.0.0"))
	if err != nil {
		t.Fatal(err)
	}
	selected := SelectedRelease{Release: release, Digest: "digest"}
	kube := &acceptanceKube{pods: []Obj{terminatedPod(`{"ok": false, "reason": "AnalysisNotAllowed"}`)}}
	c := &Controller{Kube: kube, Namespace: "pig"}
	status := Obj{}
	now := time.Date(2026, 11, 21, 0, 0, 0, 0, time.UTC)
	finish := func(outcome string) {
		name := kube.applied[len(kube.applied)-1]
		kube.job = Obj{"metadata": Obj{"name": name}, "status": Obj{"conditions": []any{Obj{"type": outcome, "status": "True"}}}}
	}
	acceptanceState := func() Obj {
		for _, v := range items(status["conditions"]) {
			if entry := object(v); str(entry, "type") == "Acceptance" {
				return entry
			}
		}
		t.Fatal("Acceptance condition missing")
		return nil
	}

	for failures, delay := range []time.Duration{2 * time.Minute, 4 * time.Minute, 8 * time.Minute} {
		if err = c.acceptance(context.Background(), d, spec, selected, "hash", status, now); err != nil {
			t.Fatal(err)
		}
		finish("Failed")
		if err = c.acceptance(context.Background(), d, spec, selected, "hash", status, now); err != nil {
			t.Fatal(err)
		}
		if got := str(status, "acceptanceNextCheck"); got != timestamp(now.Add(delay)) {
			t.Fatalf("failure %d: next check %s, want %s", failures+1, got, timestamp(now.Add(delay)))
		}
		if reason := str(acceptanceState(), "reason"); reason != "AnalysisNotAllowed" {
			t.Fatalf("failure %d: condition reason %s", failures+1, reason)
		}
		now = now.Add(delay)
		if err = c.acceptance(context.Background(), d, spec, selected, "hash", status, now); err != nil {
			t.Fatal(err)
		}
		if number(status, "acceptanceAttempt") != failures+1 {
			t.Fatalf("failure %d: attempt did not advance after the backoff", failures+1)
		}
	}

	if err = c.acceptance(context.Background(), d, spec, selected, "hash", status, now); err != nil {
		t.Fatal(err)
	}
	finish("Complete")
	if err = c.acceptance(context.Background(), d, spec, selected, "hash", status, now); err != nil {
		t.Fatal(err)
	}
	condition := acceptanceState()
	if str(condition, "status") != "True" || str(condition, "reason") != "CanonicalSuccess" {
		t.Fatalf("accepted condition: %v", condition)
	}
	if _, ok := status["acceptanceFailures"]; ok {
		t.Fatal("failure count survived acceptance")
	}
	if got := str(status, "acceptanceNextCheck"); got != timestamp(now.Add(time.Hour)) {
		t.Fatalf("accepted recheck %s", got)
	}
}
