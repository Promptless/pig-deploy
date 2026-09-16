"""Conservative compatibility for the CRD shared by independently pinned namespaces."""

from copy import deepcopy


def _schema_additive(previous: dict, target: dict) -> bool:
    """Allow optional properties and prose changes; keep every old validation invariant."""
    old, new = deepcopy(previous), deepcopy(target)
    for value in (old, new):
        value.pop("description", None)
    old_properties, new_properties = old.pop("properties", {}), new.pop("properties", {})
    if old != new:
        return False
    return all(
        name in new_properties and _schema_additive(value, new_properties[name])
        for name, value in old_properties.items()
    )


def additive_crd(previous: dict, target: dict) -> bool:
    """Refuse version removal, scope/conversion changes, or tighter existing schemas."""
    old, new = deepcopy(previous), deepcopy(target)
    old_versions, new_versions = old.pop("versions", []), new.pop("versions", [])
    # Kubernetes defaults conversion.strategy; normalize only that default.
    for value in (old, new):
        value.setdefault("conversion", {"strategy": "None"})
        value.setdefault("preserveUnknownFields", False)
        value["names"].setdefault("listKind", value["names"]["kind"] + "List")
    if old != new or len(old_versions) != len(new_versions):
        return False
    for left, right in zip(old_versions, new_versions, strict=True):
        old_schema = left.pop("schema", {}).get("openAPIV3Schema", {})
        new_schema = right.pop("schema", {}).get("openAPIV3Schema", {})
        if left != right or not _schema_additive(old_schema, new_schema):
            return False
    return True


def merge_crd(installed: dict, target: dict) -> dict | None:
    """Union compatible optional properties without removing another namespace's fields."""
    merged = deepcopy(installed)

    def add_properties(current: dict, desired: dict) -> None:
        for name, schema in desired.get("properties", {}).items():
            properties = current.setdefault("properties", {})
            if name not in properties:
                properties[name] = deepcopy(schema)
            else:
                add_properties(properties[name], schema)

    for current, desired in zip(merged.get("versions", []), target.get("versions", [])):
        add_properties(current["schema"]["openAPIV3Schema"], desired["schema"]["openAPIV3Schema"])
    # Both releases must retain every validation invariant, including required fields.
    if not additive_crd(installed, merged) or not additive_crd(target, merged):
        return None
    return merged
