package supervisor

import (
	"strings"
	"testing"
)

func TestPythonCanonicalEncoding(t *testing.T) {
	// Python json.dumps(..., sort_keys=True, separators=(",", ":")) vectors.
	for _, test := range []struct {
		value any
		want  string
	}{
		{Obj{"z": "é🐷\n\x7f", "a": []any{1, 1.0, true, nil}}, `{"a":[1,1.0,true,null],"z":"\u00e9\ud83d\udc37\n\u007f"}`},
		{0.0, "0.0"}, {1e-5, "1e-05"}, {1e-4, "0.0001"},
		{1e15, "1000000000000000.0"}, {1e16, "1e+16"}, {-1e16, "-1e+16"},
	} {
		var b strings.Builder
		canonical(&b, test.value)
		if b.String() != test.want {
			t.Errorf("%v: got %s, want %s", test.value, b.String(), test.want)
		}
	}
}
