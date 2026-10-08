package supervisor

import "reflect"

func schemaAdditive(previous, target Obj) bool {
	old, new := copyObj(previous), copyObj(target)
	delete(old, "description")
	delete(new, "description")
	op, np := child(old, "properties"), child(new, "properties")
	delete(old, "properties")
	delete(new, "properties")
	if !reflect.DeepEqual(old, new) {
		return false
	}
	for name, value := range op {
		other, ok := np[name]
		if !ok || !schemaAdditive(object(value), object(other)) {
			return false
		}
	}
	return true
}
func AdditiveCRD(previous, target Obj) bool {
	old, new := copyObj(previous), copyObj(target)
	ov, nv := items(old["versions"]), items(new["versions"])
	delete(old, "versions")
	delete(new, "versions")
	for _, v := range []Obj{old, new} {
		if _, ok := v["conversion"]; !ok {
			v["conversion"] = Obj{"strategy": "None"}
		}
		if _, ok := v["preserveUnknownFields"]; !ok {
			v["preserveUnknownFields"] = false
		}
		names := child(v, "names")
		if _, ok := names["listKind"]; !ok {
			names["listKind"] = str(names, "kind") + "List"
		}
	}
	if !reflect.DeepEqual(old, new) || len(ov) != len(nv) {
		return false
	}
	for i := range ov {
		left, right := object(ov[i]), object(nv[i])
		ls, rs := child(left, "schema", "openAPIV3Schema"), child(right, "schema", "openAPIV3Schema")
		delete(left, "schema")
		delete(right, "schema")
		if !reflect.DeepEqual(left, right) || !schemaAdditive(ls, rs) {
			return false
		}
	}
	return true
}
func addProperties(current, desired Obj) {
	for name, schema := range child(desired, "properties") {
		if current["properties"] == nil {
			current["properties"] = Obj{}
		}
		props := child(current, "properties")
		if _, ok := props[name]; !ok {
			props[name] = copyObj(object(schema))
		} else {
			addProperties(object(props[name]), object(schema))
		}
	}
}
func MergeCRD(installed, target Obj) Obj {
	merged := copyObj(installed)
	a, b := items(merged["versions"]), items(target["versions"])
	for i := 0; i < len(a) && i < len(b); i++ {
		addProperties(child(object(a[i]), "schema", "openAPIV3Schema"), child(object(b[i]), "schema", "openAPIV3Schema"))
	}
	if !AdditiveCRD(installed, merged) || !AdditiveCRD(target, merged) {
		return nil
	}
	return merged
}
