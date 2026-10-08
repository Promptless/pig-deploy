package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
)

const LeaseDuration = 300 * time.Second
const writeWindow = LeaseDuration - 30*time.Second

// WriteGate stops writes before the local, monotonic lease deadline even if a
// reconciliation outlives leadership. The reserve exceeds the API timeout.
// This is a local guard, not server-side fencing of already accepted requests.
type WriteGate struct {
	mu       sync.RWMutex
	deadline time.Time
	now      func() time.Time
}

func (g *WriteGate) time() time.Time {
	if g.now != nil {
		return g.now()
	}
	return time.Now()
}
func (g *WriteGate) renew(started time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.deadline = started.Add(writeWindow)
}
func (g *WriteGate) Revoke() { g.mu.Lock(); defer g.mu.Unlock(); g.deadline = time.Time{} }
func (g *WriteGate) remaining() time.Duration {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return g.deadline.Sub(g.time())
}

type guardedTransport struct {
	base http.RoundTripper
	gate *WriteGate
}

func (t *guardedTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if request.Method == http.MethodGet {
		return t.base.RoundTrip(request)
	}
	remaining := t.gate.remaining()
	if remaining <= 0 {
		return nil, &KubeError{Status: 409, Operation: request.Method, Resource: request.URL.Path, Reason: ": controller Lease expired"}
	}
	ctx, cancel := context.WithTimeout(request.Context(), remaining)
	// RoundTrip returns before the body is consumed; keep its context alive until
	// the caller closes the response, so streaming decoding is not interrupted.
	response, err := t.base.RoundTrip(request.Clone(ctx))
	if err != nil {
		cancel()
		return nil, err
	}
	response.Body = &cancelBody{ReadCloser: response.Body, cancel: cancel}
	return response, nil
}

// PatchLeaseLock implements client-go's election protocol with an RV-guarded
// merge patch. Existing Python installations grant patch, but not update.
type PatchLeaseLock struct {
	Client            coordinationclient.LeaseInterface
	Namespace, Holder string
	Gate              *WriteGate
	lease             *coordinationv1.Lease
}

func (l *PatchLeaseLock) Get(ctx context.Context) (*resourcelock.LeaderElectionRecord, []byte, error) {
	lease, err := l.Client.Get(ctx, "pig-supervisor", metav1.GetOptions{})
	if err != nil {
		l.Gate.Revoke()
		return nil, nil, err
	}
	l.lease = lease
	record := resourcelock.LeaseSpecToLeaderElectionRecord(&lease.Spec)
	if record.HolderIdentity != l.Holder {
		l.Gate.Revoke()
	}
	raw, err := json.Marshal(record)
	return record, raw, err
}
func (l *PatchLeaseLock) Create(ctx context.Context, record resourcelock.LeaderElectionRecord) error {
	started := l.Gate.time()
	lease, err := l.Client.Create(ctx, &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: "pig-supervisor", Namespace: l.Namespace}, Spec: resourcelock.LeaderElectionRecordToLeaseSpec(&record)}, metav1.CreateOptions{})
	return l.finish(started, record, lease, err)
}
func (l *PatchLeaseLock) Update(ctx context.Context, record resourcelock.LeaderElectionRecord) error {
	if l.lease == nil {
		return errors.New("lease not initialized")
	}
	started := l.Gate.time()
	patch, err := json.Marshal(Obj{"metadata": Obj{"resourceVersion": l.lease.ResourceVersion}, "spec": resourcelock.LeaderElectionRecordToLeaseSpec(&record)})
	if err != nil {
		return err
	}
	lease, err := l.Client.Patch(ctx, "pig-supervisor", types.MergePatchType, patch, metav1.PatchOptions{})
	return l.finish(started, record, lease, err)
}
func (l *PatchLeaseLock) finish(started time.Time, record resourcelock.LeaderElectionRecord, lease *coordinationv1.Lease, err error) error {
	if err != nil {
		l.Gate.Revoke()
		return err
	}
	l.lease = lease
	if record.HolderIdentity == l.Holder {
		l.Gate.renew(started)
	} else {
		l.Gate.Revoke()
	}
	return nil
}
func (l *PatchLeaseLock) Identity() string { return l.Holder }
func (l *PatchLeaseLock) Describe() string { return l.Namespace + "/pig-supervisor" }

// Events need additional RBAC and are unnecessary: the manager logs leadership.
func (l *PatchLeaseLock) RecordEvent(string) {}

type cancelBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *cancelBody) Close() error { defer b.cancel(); return b.ReadCloser.Close() }
