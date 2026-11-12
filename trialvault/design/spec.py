from dataclasses import dataclass
from ..validation import (
    name,
    object_value,
    list_value,
    integer,
    decimal,
    decimal_text,
    digest,
    reject,
)

MMD_SOURCES = {
    "prior_expectation",
    "clinical_relevance",
    "protocol",
    "stakeholder_decision",
    "other",
}

MECHANISMS = {"none", "batch", "specimen", "both"}


def bounded_text(value, field, maximum=512, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        reject("design", "invalid bounded text", field=field)
    return value.strip()


def seed_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        reject("design", "seed must be nonempty bounded text")
    return value.strip()


def nonnegative(value, field):
    result = decimal(value)
    if result < 0:
        reject("design", "value cannot be negative", field=field)
    return result


def rate(value, field):
    result = nonnegative(value, field)
    if result > 1:
        reject("design", "rate must lie between zero and one", field=field)
    return result


def parse_dropout(data, field="dropout"):
    """Preset loss mechanisms. Loss of a whole batch is distinct from single specimens."""
    if data is None:
        return {
            "mechanism": "none",
            "batch_rate": "0",
            "specimen_rate": "0",
            "source": None,
        }
    if isinstance(data, str):
        data = {"mechanism": data}
    object_value(
        data,
        {"mechanism", "batch_rate", "specimen_rate", "source"},
        {"mechanism"},
    )
    mechanism = data["mechanism"]
    if mechanism not in MECHANISMS:
        reject("design", "unknown dropout mechanism", field=field)
    batch_rate = rate(data.get("batch_rate", "0"), "batch_rate")
    specimen_rate = rate(data.get("specimen_rate", "0"), "specimen_rate")
    if mechanism == "none" and (batch_rate or specimen_rate):
        reject("design", "dropout mechanism none cannot carry rates", field=field)
    if mechanism == "batch" and specimen_rate:
        reject("design", "batch mechanism does not use specimen_rate", field=field)
    if mechanism == "specimen" and batch_rate:
        reject("design", "specimen mechanism does not use batch_rate", field=field)
    source = bounded_text(data.get("source"), "dropout_source", required=False)
    return {
        "mechanism": mechanism,
        "batch_rate": decimal_text(batch_rate),
        "specimen_rate": decimal_text(specimen_rate),
        "source": source,
    }


@dataclass(frozen=True)
class DesignSpec:
    id: str
    version: int
    title: str
    groups: tuple
    reference: str
    mmd: object
    mmd_source: str
    mmd_note: str
    allocation_unit: str
    block_size: int
    batch_sizes: tuple
    costs: dict
    variance: dict
    dropout: dict
    alpha: object

    @classmethod
    def parse(cls, data):
        object_value(
            data,
            {
                "id",
                "version",
                "title",
                "groups",
                "reference",
                "mmd",
                "mmd_source",
                "mmd_note",
                "allocation",
                "batch_sizes",
                "cost",
                "variance",
                "dropout",
                "alpha",
            },
            {
                "id",
                "version",
                "groups",
                "reference",
                "mmd",
                "mmd_source",
                "allocation",
                "batch_sizes",
                "cost",
                "variance",
            },
        )
        design_id = name(data["id"])
        version = integer(data["version"], 1, 100000)
        groups = list_value(data["groups"], 2, 8)
        group_names = [name(item) for item in groups]
        if len(set(group_names)) != len(group_names):
            reject("design", "duplicate treatment group")
        reference = name(data["reference"])
        if reference not in group_names:
            reject("design", "reference group is not a treatment group")
        mmd = decimal(data["mmd"])
        if mmd <= 0:
            reject("design", "minimum meaningful difference must be positive")
        mmd_source = data["mmd_source"]
        if mmd_source not in MMD_SOURCES:
            reject(
                "design",
                "minimum meaningful difference needs an explicit non-historical source",
                field="mmd_source",
            )
        mmd_note = bounded_text(data.get("mmd_note"), "mmd_note", required=False)
        allocation = data["allocation"]
        object_value(allocation, {"unit", "block_size"}, {"unit"})
        unit = allocation["unit"]
        if unit not in {"specimen", "batch"}:
            reject(
                "design", "allocation unit must be specimen or batch", field="unit"
            )
        block_size = integer(
            allocation.get("block_size", len(group_names)), 1, 10000
        )
        if block_size % len(group_names) != 0:
            reject("design", "block size must be a multiple of group count")
        if unit == "batch" and block_size != len(group_names):
            reject(
                "design",
                "batch allocation uses one block of all treatment groups",
            )
        raw_sizes = list_value(data["batch_sizes"], 1, 200)
        batch_sizes = tuple(integer(size, 1, 1000) for size in raw_sizes)
        if unit == "batch" and len(batch_sizes) < 2 * len(group_names):
            reject(
                "design",
                "batch allocation needs at least two batches per treatment group",
                minimum=2 * len(group_names),
                batches=len(batch_sizes),
            )
        if sum(batch_sizes) > 20000:
            reject("design", "planned specimen count exceeds limit")
        cost = data["cost"]
        object_value(cost, {"fixed", "per_batch", "per_specimen"})
        costs = {
            "fixed": decimal_text(nonnegative(cost.get("fixed", "0"), "fixed")),
            "per_batch": decimal_text(
                nonnegative(cost.get("per_batch", "0"), "per_batch")
            ),
            "per_specimen": decimal_text(
                nonnegative(cost.get("per_specimen", "0"), "per_specimen")
            ),
        }
        variance = parse_variance(data["variance"])
        dropout = parse_dropout(data.get("dropout"))
        alpha = decimal(data.get("alpha", "0.05"))
        if not 0 < alpha < 0.5:
            reject("design", "alpha must lie strictly between zero and 0.5")
        return cls(
            design_id,
            version,
            bounded_text(data.get("title"), "title", required=False) or design_id,
            tuple(group_names),
            reference,
            mmd,
            mmd_source,
            mmd_note,
            unit,
            block_size,
            batch_sizes,
            costs,
            variance,
            dropout,
            alpha,
        )

    def as_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "groups": list(self.groups),
            "reference": self.reference,
            "mmd": decimal_text(self.mmd),
            "mmd_source": self.mmd_source,
            "mmd_note": self.mmd_note,
            "allocation": {
                "unit": self.allocation_unit,
                "block_size": self.block_size,
            },
            "batch_sizes": list(self.batch_sizes),
            "cost": dict(self.costs),
            "variance": dict(self.variance),
            "dropout": dict(self.dropout),
            "alpha": decimal_text(self.alpha),
        }

    @property
    def fingerprint(self):
        return digest(self.as_dict())

    def planned_cost(self):
        from decimal import Decimal

        total = (
            decimal(self.costs["fixed"])
            + decimal(self.costs["per_batch"]) * len(self.batch_sizes)
            + decimal(self.costs["per_specimen"]) * sum(self.batch_sizes)
        )
        return decimal_text(total)

    def summary(self):
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "digest": self.fingerprint,
            "groups": list(self.groups),
            "reference": self.reference,
            "allocation": self.allocation_unit,
            "batches": len(self.batch_sizes),
            "specimens": sum(self.batch_sizes),
            "cost": self.planned_cost(),
            "mmd": decimal_text(self.mmd),
            "mmd_source": self.mmd_source,
        }


def parse_variance(data):
    if not isinstance(data, dict):
        reject("design", "variance input must be an object")
    kind = data.get("kind")
    if kind == "historical":
        object_value(
            data,
            {
                "kind",
                "dataset",
                "version",
                "value_field",
                "batch_field",
                "group_field",
                "note",
            },
            {"kind", "dataset", "version", "value_field", "batch_field"},
        )
        group_field = data.get("group_field")
        return {
            "kind": "historical",
            "dataset": name(data["dataset"]),
            "version": integer(data["version"], 1),
            "value_field": name(data["value_field"]),
            "batch_field": name(data["batch_field"]),
            "group_field": name(group_field) if group_field is not None else None,
            "note": bounded_text(data.get("note"), "variance_note", required=False),
        }
    if kind == "assumptions":
        object_value(
            data,
            {
                "kind",
                "between_batch_variance",
                "within_batch_variance",
                "source",
                "note",
            },
            {"kind", "between_batch_variance", "within_batch_variance", "source"},
        )
        between = nonnegative(
            data["between_batch_variance"], "between_batch_variance"
        )
        within = nonnegative(data["within_batch_variance"], "within_batch_variance")
        if between + within <= 0:
            reject("design", "variance assumptions need positive total variance")
        return {
            "kind": "assumptions",
            "between_batch_variance": decimal_text(between),
            "within_batch_variance": decimal_text(within),
            "source": bounded_text(data["source"], "variance_source"),
            "note": bounded_text(data.get("note"), "variance_note", required=False),
        }
    reject("design", "variance kind must be historical or assumptions")


def validate_sources(spec, registry):
    """Structural checks against registered datasets. Does not inspect outcomes."""
    variance = spec.variance
    if variance["kind"] != "historical":
        return None
    record = registry.version(variance["dataset"], variance["version"])
    if record["state"] != "active":
        reject("state", "historical variance source version is withdrawn")
    dataset = registry.dataset(variance["dataset"])
    schema = registry.schema(dataset["schema_id"], record["schema_version"])
    value = schema.by_name.get(variance["value_field"])
    batch = schema.by_name.get(variance["batch_field"])
    if value is None or value.type not in {"integer", "decimal"}:
        reject("design", "historical value field must be numeric")
    if batch is None or batch.type not in {"text", "integer", "enum"}:
        reject("design", "historical batch field must be a scalar identifier")
    group_field = variance.get("group_field")
    if group_field is not None:
        group = schema.by_name.get(group_field)
        if group is None or group.type not in {"text", "integer", "enum"}:
            reject("design", "historical group field must be a scalar identifier")
    return {
        "dataset": variance["dataset"],
        "version": variance["version"],
        "digest": record["digest"],
    }
