from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def title_units(value):
    return sum(1 if ord(c) < 128 else 2 for c in value)


@dataclass
class Issue:
    path: str
    reason: str
    source: str = "not supplied"
    blocking: bool = True

    def to_dict(self):
        return asdict(self)


class Product:
    def __init__(self, data, base=None, media_digest=None):
        self.data = copy.deepcopy(data)
        self.base = Path(base or ".").resolve()
        self.media_digest = media_digest or {}

    @property
    def task_id(self):
        return self.data.get("task_id", "")

    @property
    def version(self):
        return hashlib.sha256(canonical({"data": self.data, "media": self.media_digest}).encode()).hexdigest()

    def facts(self):
        result = {k: self.data.get(k, {}) for k in ("shop", "category")}
        for section in ("fields", "logistics"):
            result.update({f"{section}.{k}": v for k, v in self.data.get(section, {}).items()})
        for i, sku in enumerate(self.data.get("skus", [])):
            result.update({f"skus.{i}.{k}": v for k, v in sku.items()})
        return result

    def fact(self, path):
        v = self.facts().get(path, {})
        return v if isinstance(v, dict) else {"value": None, "reason": "field must include provenance"}

    def value(self, path):
        return self.fact(path).get("value")

    def known_paths(self):
        blocked = {i.path for i in validate(self) if i.blocking}
        return [p for p, f in self.facts().items() if isinstance(f, dict)
                and f.get("value") is not None and f.get("confirmed") is True
                and f.get("source") and not f.get("conflict") and p not in blocked
                and not any(p.startswith(b + ".") for b in blocked)]

    @property
    def total_inventory(self):
        return sum(int(self.value(f"skus.{i}.inventory")) for i in range(len(self.data.get("skus", []))))

    def expected(self):
        values = {p: self.value(p) for p in self.known_paths()}
        for i in range(len(self.data.get("skus", []))):
            path = f"skus.{i}.pack_quantity"
            if path not in values:
                values[path] = None
        for i in range(len(self.data.get("skus", []))):
            p = f"skus.{i}.price"
            if p in values:
                values[p] = str(Decimal(str(values[p])).quantize(Decimal("0.01")))
        if not any(i.path.endswith("inventory") and i.blocking for i in validate(self)):
            values["total_inventory"] = self.total_inventory
        values["title_count"] = 60
        values["guide_title_count"] = 30
        values["sku_names"] = [self.value(f"skus.{i}.name") for i in range(len(self.data.get("skus", [])))]
        values["main_images"] = self.data.get("media", {}).get("main", [])
        values["detail_images"] = self.data.get("media", {}).get("detail", [])
        return values


def validate(product):
    d = product.data
    issues = []

    def add(path, reason, blocking=True):
        f = product.fact(path)
        issues.append(Issue(path, reason, str(f.get("source") or "not supplied"), blocking))

    if not d.get("task_id"):
        add("task_id", "stable task identifier required")
    policy = d.get("title_policy")
    if policy is not None and policy != {"title_units": 60, "guide_units": 30, "forbid_brand": True}:
        add("title_policy", "first version cannot lower the global 60/30 no-brand title rule")
    required = ["shop", "category", "fields.title", "fields.guide_title", "fields.brand",
                "fields.installation", "fields.conductor", "fields.model"]
    required += [f"logistics.{p}" for p in ("province", "city", "dispatch", "template", "listing")]
    for i, sku in enumerate(d.get("skus", [])):
        required += [f"skus.{i}.{k}" for k in ("name", "image", "price", "inventory",
                                               "sales_unit", "inventory_unit", "area", "cable")]
    for p in required:
        f = product.fact(p)
        if f.get("value") in (None, "") or f.get("confirmed") is not True or not f.get("source"):
            add(p, f.get("reason") or "missing value, confirmation or source")
    for p, f in product.facts().items():
        if isinstance(f, dict) and f.get("conflict"):
            add(p, "conflicting sources: " + str(f["conflict"]))
    if product.value("category") not in (None, "50021001"):
        add("category", "first version only adapts category 50021001")
    names = [product.value(f"skus.{i}.name") for i in range(len(d.get("skus", [])))]
    if not names or len(names) != len(set(names)):
        add("skus", "empty or duplicated SKU names; no automatic overwrite")
    for i, sku in enumerate(d.get("skus", [])):
        p = f"skus.{i}"
        value = product.value(p + ".price")
        if value is not None:
            try:
                price = Decimal(str(value))
                if not price.is_finite() or price <= 0 or price.as_tuple().exponent < -2:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                add(p + ".price", "price must be positive, finite and exact to cents")
        stock = product.value(p + ".inventory")
        if stock is not None and (type(stock) is not int or stock < 0):
            add(p + ".inventory", "inventory must be a nonnegative integer")
        if product.value(p + ".sales_unit") != product.value(p + ".inventory_unit"):
            add(p + ".inventory_unit", "sales and inventory units differ; no inferred conversion")
        quantity = product.value(p + ".pack_quantity")
        if quantity is None:
            add(p + ".pack_quantity", product.fact(p + ".pack_quantity").get("reason") or
                "optional packaging quantity has no evidence; leave blank", False)
        elif type(quantity) is not int or quantity <= 0:
            add(p + ".pack_quantity", "pack quantity must be a positive integer")
    brands = [product.value("fields.brand")] + d.get("forbidden_title_terms", [])
    for p, length in (("fields.title", 60), ("fields.guide_title", 30)):
        value = product.value(p)
        if value is not None:
            if not isinstance(value, str) or title_units(value) != length:
                add(p, f"title must count {length} platform units; actual page must also be checked")
            if any(b and b.casefold() in str(value).casefold() for b in brands):
                add(p, "title contains brand/trademark")
    if not d.get("title_facts") or any(not f.get("source") or f.get("confirmed") is not True
                                       for f in d.get("title_facts", [])):
        add("title_facts", "confirmed factual title basis required; never pad model text")
    if product.value("logistics.listing") not in (None, "放入仓库"):
        add("logistics.listing", "listing must be warehouse")
    for kind in ("main", "detail"):
        files = d.get("media", {}).get(kind, [])
        if not files or len(files) != len(set(files)):
            add(f"media.{kind}", "explicit unique ordered image list required")
    for kind in ("main", "detail", "sku"):
        if d.get('media', {}).get('input_mode') != 'local_folder' and not d.get("media", {}).get("folders", {}).get(kind):
            add(f"media.folders.{kind}", "explicit platform folder required")
    media = d.get('media', {})
    if media.get('input_mode') == 'local_folder':
        auth = media.get('local_folder_authorization') or {}
        root = media.get('root')
        if (not isinstance(auth, dict) or not isinstance(root, str) or not isinstance(auth.get('root'), str)
                or auth.get('confirmed') is not True or not auth.get('source')
                or (product.base / auth['root']).resolve() != (product.base / root).resolve()):
            add('media.local_folder_authorization', 'current folder upload authorization with source required')
    elif media.get('input_mode') not in (None, 'material_center'):
        add('media.input_mode', 'supported image source required')
    return issues


def load_product(path):
    path = Path(path).resolve()
    with path.open(encoding="utf-8-sig") as f:
        data = json.load(f)
    product = Product(data, path.parent)
    from .media import manifest
    report = manifest(product)
    product.media_digest = report["files"]
    return product
