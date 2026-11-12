from ..validation import decimal, decimal_text, object_value, reject


def resolved_components(components):
    return (
        float(decimal(components["between_batch_variance"])),
        float(decimal(components["within_batch_variance"])),
    )


def icc_of(between, within):
    total = between + within
    if total <= 0:
        reject("design", "variance components have zero total")
    return between / total


def _effects(spec, option):
    """Contrast effects for non-reference groups. Historical data is never used.

    Every scenario states the difference from the reference group explicitly;
    the reference group is always zero by construction.
    """
    if option is None:
        return {
            group: (0.0 if group == spec.reference else float(spec.mmd))
            for group in spec.groups
        }
    object_value(option, {"kind", "values", "scale"}, {"kind"})
    kind = option["kind"]
    if kind == "mmd":
        scale = decimal(option.get("scale", "1"))
        if scale <= 0:
            reject("design", "effect scale must be positive")
        return {
            group: (
                0.0
                if group == spec.reference
                else float(spec.mmd) * float(scale)
            )
            for group in spec.groups
        }
    if kind == "null":
        return {group: 0.0 for group in spec.groups}
    if kind == "custom":
        values = option.get("values")
        value_map = _parse_effect_map(spec, values)
        return value_map
    reject("design", "unknown effect assignment kind")


def _parse_effect_map(spec, values):
    if not isinstance(values, dict) or not values:
        reject("design", "custom effect assignment needs group values")
    unknown = values.keys() - set(spec.groups)
    if unknown:
        reject("design", "custom effect names unknown group", groups=sorted(unknown))
    result = {}
    for group in spec.groups:
        supplied = decimal(values.get(group, "0"))
        if group == spec.reference:
            if supplied != 0:
                reject("design", "reference group effect must be zero")
            result[group] = 0.0
        else:
            result[group] = float(supplied)
    if all(result[group] == 0.0 for group in spec.groups if group != spec.reference):
        reject("design", "custom effects cannot be all zero; use kind null")
    return result


def validate_effects(spec, option):
    """Validate the effect assignment once, before any replicate runs."""
    _effects(spec, option)


def scenario(spec, components, key, label, aim, dropout, effects=None):
    between, within = resolved_components(components)
    return {
        "key": key,
        "label": label,
        "aim": aim,
        "between_batch_variance": between,
        "within_batch_variance": within,
        "icc": between / (between + within),
        "group_effects": _effects(spec, effects),
        "effect": _effect_label(spec, effects),
        "dropout": dropout,
        "seed_component": key,
    }


def _effect_label(spec, effects):
    if (effects or {}).get("kind") == "custom":
        return "custom"
    if (effects or {}).get("kind") == "null":
        return "0"
    scale = decimal((effects or {}).get("scale", "1"))
    return decimal_text(spec.mmd * scale)


def dropout_values(dropout, multiplier=1.0):
    return {
        "mechanism": dropout["mechanism"],
        "batch_rate": min(1.0, float(decimal(dropout["batch_rate"])) * multiplier),
        "specimen_rate": min(1.0, float(decimal(dropout["specimen_rate"])) * multiplier),
    }


def primary_scenarios(spec, components, options):
    """Null scenario for type-I verification plus one alternative scenario."""
    result = {}
    dropout = dropout_values(spec.dropout)
    null = scenario(spec, components, "null", "no treatment effect", "null", dropout, {"kind": "null"})
    result["null"] = null
    alternative = scenario(
        spec,
        components,
        "alternative",
        "design target under stated assumptions",
        "alternative",
        dropout,
        options.get("effects"),
    )
    result["alternative"] = alternative
    return result


def sensitivity_scenarios(spec, components, options):
    """Cells varying intra-batch correlation and loss rates around the base case."""
    settings = options.get("sensitivity") or {}
    object_value(settings, {"icc", "dropout_multiplier"}, set())
    icc_values = settings.get("icc")
    multipliers = settings.get("dropout_multiplier")
    if icc_values is None and multipliers is None:
        return []
    between, within = resolved_components(components)
    total = between + within
    base_icc = between / total
    if icc_values is None:
        icc_values = [base_icc]
    else:
        icc_values = [_rate(value, "icc") for value in _list(icc_values)]
        if any(value == 0.0 for value in icc_values):
            reject("design", "icc sensitivity cannot be zero")
    if multipliers is None:
        multipliers = [1.0]
    else:
        multipliers = [_multiplier(value) for value in _list(multipliers)]
    cells = []
    for icc in icc_values:
        for multiplier in multipliers:
            cell_components = {
                "between_batch_variance": _trim(total * icc),
                "within_batch_variance": _trim(total * (1.0 - icc)),
            }
            key = "icc={}/dropout={}".format(
                _trim(icc), _trim(multiplier)
            )
            alternative = scenario(
                spec,
                cell_components,
                key,
                "sensitivity cell",
                "alternative",
                dropout_values(spec.dropout, multiplier),
                options.get("effects"),
            )
            cells.append(alternative)
    return cells


def _list(value):
    if not isinstance(value, list) or not value or len(value) > 32:
        reject("design", "sensitivity grid must be a bounded nonempty list")
    return value


def _rate(value, field):
    result = decimal(value)
    if not 0 < result < 1:
        reject("design", "sensitivity icc must lie strictly between zero and one", field=field)
    return float(result)


def _multiplier(value):
    result = decimal(value)
    if result < 0 or result > 10:
        reject("design", "dropout multiplier outside allowed range")
    return float(result)


def _trim(value):
    text = format(decimal(str(value)))
    return text
