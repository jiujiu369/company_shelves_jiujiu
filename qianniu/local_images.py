from pathlib import Path

from .data import validate
from .media import ensure_version, manifest


def upload_plan(product):
    """Resolve only explicitly configured images; directory discovery is never consent."""
    ensure_version(product)
    media = product.data.get('media', {})
    inventory = manifest(product)
    root = Path(inventory['root']).resolve()
    auth = media.get('local_folder_authorization') or {}
    if not isinstance(auth, dict):
        auth = {}
    authorized = (media.get('input_mode') == 'local_folder' and auth.get('confirmed') is True
                  and bool(auth.get('source')) and isinstance(auth.get('root'), str)
                  and (product.base / auth['root']).resolve() == root)
    issues = []
    if not authorized:
        issues.append({'path': 'media.local_folder_authorization',
                       'reason': 'current folder upload authorization with source required', 'blocking': True})
    groups = {'main': [], 'detail': [], 'sku': []}
    ready = {kind: authorized for kind in groups}
    for kind in ('main', 'detail'):
        names = media.get(kind, [])
        seen = set()
        if not names:
            ready[kind] = False
        for name in names:
            path = (root / name).resolve()
            if path in seen:
                ready[kind] = False
                issues.append({'path': 'media.' + kind, 'reason': 'duplicate image path or alias', 'blocking': True})
            seen.add(path)
            groups[kind].append({'file': name, 'path': str(path), **inventory['files'].get(name, {})})
    known = product.known_paths()
    for i in range(len(product.data.get('skus', []))):
        name, image = product.value(f'skus.{i}.name'), product.value(f'skus.{i}.image')
        if f'skus.{i}.name' not in known or f'skus.{i}.image' not in known:
            ready['sku'] = False
            issues.append({'path': f'skus.{i}.image', 'reason': 'confirmed SKU/name image mapping required', 'blocking': True})
            continue
        groups['sku'].append({'sku': name, 'file': image, 'path': str((root / image).resolve()),
                              **inventory['files'].get(image, {})})
    if not groups['sku'] or any(i.path == 'skus' and i.blocking for i in validate(product)):
        ready['sku'] = False
    for kind, rows in groups.items():
        for row in rows:
            related = [i for i in inventory['issues'] if i['path'] == row['file']]
            if related or not row.get('sha256'):
                ready[kind] = False
                issues.extend(related)
                if not related:
                    issues.append({'path': row['file'], 'reason': 'image content/version unavailable', 'blocking': True})
    # Same basename with different content is unsafe for name-based platform mapping.
    names = {}
    for kind, rows in groups.items():
        for row in rows:
            key = Path(row['file']).name.casefold()
            if key in names and names[key][1] != row.get('sha256'):
                ready[kind] = ready[names[key][0]] = False
                issues.append({'path': row['file'], 'reason': 'same filename has conflicting image content', 'blocking': True})
            names[key] = (kind, row.get('sha256'))
    if len(groups['main']) > 5:
        ready['main'] = False
        issues.append({'path': 'media.main', 'reason': 'first version supports at most five main images', 'blocking': True})
    return {'version': product.version, 'root': str(root), 'groups': groups, 'group_ready': ready,
            'ready': all(ready.values()), 'issues': issues, 'extras': inventory['extras'],
            'authorization': auth, 'platform_saved': False}
