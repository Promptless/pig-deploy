package supervisor

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
)

// Obj is used at the Kubernetes unstructured boundary and for durable status,
// whose unknown fields must survive upgrades between independently pinned releases.
type Obj = map[string]any

func object(v any) Obj {
	m, _ := v.(map[string]any)
	if m == nil {
		return Obj{}
	}
	return m
}
func child(m Obj, keys ...string) Obj {
	for _, k := range keys {
		m = object(m[k])
	}
	return m
}
func str(m Obj, k string) string   { v, _ := m[k].(string); return v }
func boolean(m Obj, k string) bool { v, _ := m[k].(bool); return v }
func items(v any) []any            { a, _ := v.([]any); return a }
func number(m Obj, k string) int {
	switch n := m[k].(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}
func copyObj(m Obj) Obj {
	var out Obj
	if err := decode(m, &out); err != nil {
		panic(err)
	}
	if out == nil {
		return Obj{}
	}
	return out
}
func asObj(v any) Obj {
	var m Obj
	if err := decode(v, &m); err != nil {
		panic(err)
	}
	return m
}
func contains(values []string, value string) bool {
	for _, v := range values {
		if v == value {
			return true
		}
	}
	return false
}

// CanonicalDigest retains Python json.dumps(sort_keys=True, ensure_ascii=True)
// hashes so existing configuration hashes and immutable Job names survive handoff.
// Callers use JSON-compatible values; floats must remain floats (not integer JSON).
func CanonicalDigest(value any) string {
	var b strings.Builder
	canonical(&b, value)
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}
func canonical(b *strings.Builder, value any) {
	switch v := value.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(v))
	case string:
		b.WriteByte('"')
		for _, r := range v {
			switch r {
			case '"':
				b.WriteString(`\"`)
			case '\\':
				b.WriteString(`\\`)
			case '\b':
				b.WriteString(`\b`)
			case '\f':
				b.WriteString(`\f`)
			case '\n':
				b.WriteString(`\n`)
			case '\r':
				b.WriteString(`\r`)
			case '\t':
				b.WriteString(`\t`)
			default:
				if r < 32 || r > 126 {
					if r > 0xffff {
						a, c := utf16.EncodeRune(r)
						fmt.Fprintf(b, `\u%04x\u%04x`, a, c)
					} else {
						fmt.Fprintf(b, `\u%04x`, r)
					}
				} else {
					b.WriteRune(r)
				}
			}
		}
		b.WriteByte('"')
	case int:
		b.WriteString(strconv.Itoa(v))
	case int64:
		b.WriteString(strconv.FormatInt(v, 10))
	case json.Number:
		b.WriteString(v.String())
	case float64:
		b.WriteString(pythonFloat(v))
	case []any:
		b.WriteByte('[')
		for i, x := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			canonical(b, x)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			canonical(b, k)
			b.WriteByte(':')
			canonical(b, v[k])
		}
		b.WriteByte('}')
	default:
		data, err := json.Marshal(v)
		if err != nil {
			panic(err)
		}
		var normalized any
		d := json.NewDecoder(strings.NewReader(string(data)))
		d.UseNumber()
		if err := d.Decode(&normalized); err != nil {
			panic(err)
		}
		canonical(b, normalized)
	}
}

func pythonFloat(v float64) string {
	s := strconv.FormatFloat(v, 'g', -1, 64)
	// Python uses fixed notation for exponents -4 through 15.
	if v != 0 && (v >= 1e-4 && v < 1e16 || v <= -1e-4 && v > -1e16) {
		s = strconv.FormatFloat(v, 'f', -1, 64)
	}
	if !strings.ContainsAny(s, ".e") {
		s += ".0"
	}
	return s
}
