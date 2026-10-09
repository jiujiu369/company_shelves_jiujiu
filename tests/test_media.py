from pathlib import Path

from PIL import Image

from qianniu.data import Product, load_product
from qianniu.media import manifest, map_candidates, verify_segments


def test_full_filename_mapping_detects_repeated_names_with_distinct_content():
    candidates = [{"folder": "JG/sku", "name": "施科特_SKU_JG6-6.png", "sha256": "a"},
                  {"folder": "JG/sku", "name": "施科特_SKU_JG6-6.png", "sha256": "b"},
                  {"folder": "other", "name": "施科特_SKU_JG6-6.png", "sha256": "a"}]
    result = map_candidates("JG/sku", "施科特_SKU_JG6-6.png", "a", candidates)
    assert result["status"] == "conflict"


def test_extra_images_are_not_new_skus():
    candidates = [{"folder": "JG/sku", "name": "施科特_SKU_JG6-6.png", "sha256": "a"},
                  {"folder": "JG/sku", "name": "detail.png", "sha256": "b"}]
    result = map_candidates("JG/sku", "施科特_SKU_JG6-6.png", "a", candidates)
    assert result["status"] == "unique" and len(result["extras"]) == 1


def test_unknown_content_cannot_be_verified():
    r = map_candidates("JG/sku", "sku.png", "a", [{"folder": "JG/sku", "name": "sku.png"}])
    assert r["status"] == "unverified_content"


def test_manifest_hash_changes_when_image_changes(tmp_path):
    p = tmp_path / "main.png"
    Image.new("RGB", (10, 20), "red").save(p)
    product = Product({"media": {"root": str(tmp_path), "main": ["main.png"], "detail": []}, "skus": []})
    first = manifest(product)["files"]
    Image.new("RGB", (10, 20), "green").save(p)
    assert first != manifest(product)["files"]


def test_long_image_reports_split_without_modifying_original(tmp_path):
    p = tmp_path / "detail.png"
    Image.new("RGB", (750, 1502), "red").save(p)
    original = p.read_bytes()
    product = Product({"media": {"root": str(tmp_path), "main": [], "detail": ["detail.png"]}, "skus": []})
    report = manifest(product)
    assert report["files"]["detail.png"]["split_required"] is True
    assert p.read_bytes() == original


def test_segments_must_reproduce_every_pixel_in_order():
    original = Image.new("RGB", (4, 8), "red")
    original.paste("green", (0, 4, 4, 8))
    a, b = original.crop((0, 0, 4, 4)), original.crop((0, 4, 4, 8))
    assert verify_segments(original, [a, b])
    assert not verify_segments(original, [b, a])
    assert not verify_segments(original, [a])


def test_outside_root_image_reports_issue_without_scanning_other_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    Image.new("RGB", (10, 10), "red").save(other / "outside.png")
    Image.new("RGB", (10, 10), "blue").save(other / "private-extra.png")
    p = Product({"media": {"root": str(root), "main": ["../other/outside.png"]}, "skus": []})
    result = manifest(p)
    assert result["issues"][0]["blocking"] and result["extras"] == []
