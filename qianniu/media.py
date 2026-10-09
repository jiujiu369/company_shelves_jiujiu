from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageChops


def manifest(product):
    media = product.data.get("media", {})
    root = (product.base / media.get("root", ".")).resolve()
    names = list(media.get("main", [])) + list(media.get("detail", []))
    names += [product.value(f"skus.{i}.image") for i in range(len(product.data.get("skus", [])))]
    files, issues = {}, []
    for name in dict.fromkeys(n for n in names if n):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            issues.append({"path": name, "reason": "image escapes selected root", "blocking": True})
            continue
        try:
            payload = path.read_bytes()
            with Image.open(path) as im:
                im.load()
                width, height = im.size
                files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "width": width,
                               "height": height, "bytes": len(payload), "format": im.format,
                               "split_required": name in media.get("detail", []) and height > width * 2}
            if files[name]["split_required"] and not media.get("split_authorized"):
                issues.append({"path": name, "reason": "long detail requires explicit split authorization",
                               "blocking": True})
        except (OSError, ValueError) as e:
            files[name] = {"error": str(e)}
            issues.append({"path": name, "reason": str(e), "blocking": True})
    selected = {(root / n).resolve() for n in names if n and (root / n).resolve().is_relative_to(root)}
    directories = {p.parent for p in selected}
    extras = [str(p.relative_to(root)) for directory in directories if directory.is_dir()
              for p in directory.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
              and p.resolve() not in selected]
    return {"root": str(root), "files": files, "issues": issues, "extras": sorted(extras)}


def map_candidates(folder, filename, expected_hash, candidates):
    selected = [c for c in candidates if c.get("folder") == folder and c.get("name") == filename]
    extras = [c for c in candidates if c.get("folder") == folder and c.get("name") != filename]
    if len(selected) > 1:
        status = "conflict"
    elif not selected:
        status = "missing"
    elif not selected[0].get("sha256"):
        status = "unverified_content"
    elif selected[0]["sha256"] != expected_hash:
        status = "content_mismatch"
    else:
        status = "unique"
    return {"status": status, "candidates": selected, "extras": extras}


def ensure_version(product):
    from .browser import PlatformPause
    if product.media_digest and manifest(product)["files"] != product.media_digest:
        raise PlatformPause("image changed after product version was loaded")


def verify_segments(original, segments):
    if not segments or any(s.width != original.width for s in segments):
        return False
    if sum(s.height for s in segments) != original.height:
        return False
    joined = Image.new("RGB", original.size)
    y = 0
    for s in segments:
        joined.paste(s.convert("RGB"), (0, y))
        y += s.height
    return ImageChops.difference(original.convert("RGB"), joined).getbbox() is None


def same_pixels(payload, path):
    from io import BytesIO
    with Image.open(BytesIO(payload)) as remote, Image.open(path) as local:
        return remote.size == local.size and ImageChops.difference(
            remote.convert("RGB"), local.convert("RGB")).getbbox() is None
