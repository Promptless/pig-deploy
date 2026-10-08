package supervisor

import (
	"bufio"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

type transcript struct {
	Test            string `json:"test"`
	Deployment      Obj    `json:"deployment"`
	Population      int    `json:"population"`
	Now             string `json:"now"`
	Namespace       string `json:"namespace"`
	SystemNamespace string `json:"systemNamespace"`
	SupervisorName  string `json:"supervisorName"`
	CatalogURL      string `json:"catalogURL"`
	Events          []Obj  `json:"events"`
	Error           string `json:"error"`
}
type replay struct {
	t      *testing.T
	events []Obj
	index  int
}

func (r *replay) next(operation string) Obj {
	r.t.Helper()
	if r.index >= len(r.events) {
		r.t.Fatalf("unexpected %s after transcript finished", operation)
	}
	event := r.events[r.index]
	r.index++
	if str(event, "operation") != operation {
		r.t.Fatalf("event %d: expected %s, got %s", r.index, str(event, "operation"), operation)
	}
	return event
}
func (r *replay) call(operation string, args ...any) (any, error) {
	r.t.Helper()
	e := r.next(operation)
	expected := items(e["args"])
	if operation == "list" {
		if len(expected) == 2 {
			expected = append(expected, "")
		}
		if v, ok := child(e, "kwargs")["selector"]; ok {
			expected[2] = v
		}
	}
	if operation == "request" {
		expected = append(expected, child(e, "kwargs")["json"])
	}
	a, b := normalize(expected), normalize(args)
	if !reflect.DeepEqual(a, b) {
		x, _ := json.MarshalIndent(a, "", "  ")
		y, _ := json.MarshalIndent(b, "", "  ")
		r.t.Fatalf("event %d %s arguments differ\nPython: %s\nGo: %s", r.index, operation, x, y)
	}
	if failure, ok := e["error"]; ok {
		return nil, &KubeError{Status: number(object(failure), "status")}
	}
	return e["result"], nil
}
func (r *replay) Get(_ context.Context, kind, ns, name string) (Obj, error) {
	v, e := r.call("get", kind, ns, name)
	if v == nil {
		return nil, e
	}
	return object(v), e
}
func (r *replay) List(_ context.Context, kind, ns, selector string) ([]Obj, error) {
	v, e := r.call("list", kind, ns, selector)
	out := []Obj{}
	for _, x := range items(v) {
		out = append(out, object(x))
	}
	return out, e
}
func (r *replay) Apply(_ context.Context, o Obj) (Obj, error) {
	v, e := r.call("apply", o)
	return object(v), e
}
func (r *replay) Patch(_ context.Context, kind, ns, name string, o Obj) (Obj, error) {
	v, e := r.call("patch", kind, ns, name, o)
	return object(v), e
}
func (r *replay) Status(_ context.Context, d, s Obj) error { _, e := r.call("status", d, s); return e }
func (r *replay) DeleteOwnedIngress(_ context.Context, ns, name, owner string) (bool, error) {
	v, e := r.call("delete_owned_ingress", ns, name, owner)
	return v == true, e
}
func (r *replay) Request(_ context.Context, method, path string, o Obj) (Obj, error) {
	v, e := r.call("request", method, path, o)
	return object(v), e
}
func (r *replay) RoundTrip(req *http.Request) (*http.Response, error) {
	e := r.next("http")
	if req.URL.String() != str(e, "url") {
		r.t.Fatalf("HTTP URL: want %s, got %s", str(e, "url"), req.URL)
	}
	return &http.Response{StatusCode: number(e, "status"), Header: http.Header{}, Body: io.NopCloser(strings.NewReader(str(e, "body"))), Request: req}, nil
}

// Only presentation differences are normalized. Digests, names, phase order,
// migration hazards, owner references and all resource writes remain exact.
func normalize(v any) any {
	b, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(b, &out)
	var walk func(any) any
	walk = func(x any) any {
		switch y := x.(type) {
		case map[string]any:
			for k, z := range y {
				y[k] = walk(z)
			}
			if str(y, "reason") == "InvalidConfiguration" {
				y["message"] = "invalid configuration"
			}
			if str(y, "name") == "PIG_REQUIREMENTS" {
				var parsed any
				if json.Unmarshal([]byte(str(y, "value")), &parsed) == nil {
					y["value"] = parsed
				}
			}
		case []any:
			for i, z := range y {
				y[i] = walk(z)
			}
		case string:
			if t, e := time.Parse(time.RFC3339Nano, y); e == nil {
				return t.UTC().Format(time.RFC3339Nano)
			}
		}
		return x
	}
	return walk(out)
}
func TestPythonReconciliationCompatibility(t *testing.T) {
	file, err := os.Open("testdata/python.jsonl.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 4096), 2<<20)
	count := 0
	for scanner.Scan() {
		var record transcript
		if err = json.Unmarshal(scanner.Bytes(), &record); err != nil {
			t.Fatal(err)
		}
		count++
		t.Run(fmt.Sprintf("%04d/%s", count, strings.TrimPrefix(record.Test, "tests/test_supervisor.py::")), func(t *testing.T) {
			r := &replay{t: t, events: record.Events}
			catalog := NewCatalog(record.CatalogURL)
			catalog.Client.Transport = r
			c := &Controller{Kube: r, Catalog: catalog, Namespace: record.Namespace, SystemNamespace: record.SystemNamespace, SupervisorName: record.SupervisorName}
			for _, e := range record.Events {
				if str(e, "operation") == "status" {
					args := items(e["args"])
					id := str(object(args[1]), "transitionID")
					c.NewTransitionID = func() string { return id }
				}
			}
			now, err := time.Parse(time.RFC3339Nano, record.Now)
			if err != nil {
				t.Fatal(err)
			}
			err = c.Reconcile(context.Background(), record.Deployment, record.Population, now)
			if (err != nil) != (record.Error != "") {
				t.Fatalf("expected error %q, got %v", record.Error, err)
			}
			if r.index != len(r.events) {
				t.Fatalf("consumed %d of %d events", r.index, len(r.events))
			}
		})
	}
	if err = scanner.Err(); err != nil {
		t.Fatal(err)
	}
	if count != 1569 {
		t.Fatalf("expected 1569 reconciliations, got %d", count)
	}
}
