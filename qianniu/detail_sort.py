"""Order-only planning never grants material or submission acceptance."""
import re

from .browser import PlatformPause


def image_key(src):
    return re.sub(r'(?i)(\.(?:png|jpe?g))_\d+x\d+q\d+_\.webp$', r'\1', src)


def sort_plan(audit, authorization):
    if not authorization or not authorization.strip():
        raise PlatformPause('direct user authorization required for order-only correction')
    expected = audit['expected_names']
    if len(expected) != len(set(expected)):
        raise PlatformPause('duplicate expected detail image')
    mapping, current_names, current_keys = {}, [], []
    for group in audit['groups']:
        matches = group.get('matches', [])
        if len(matches) == 1:
            name = matches[0]
        else:
            candidates = sorted(group.get('similarity_candidates', []), key=lambda c: c['rms'])
            if (not candidates or candidates[0]['rms'] > 5 or
                    len(candidates) > 1 and candidates[1]['rms'] - candidates[0]['rms'] < 20):
                raise PlatformPause('detail identity ambiguous; preserve current order')
            name = candidates[0]['name']
        if name not in expected or name in mapping:
            raise PlatformPause('missing, duplicate or additional detail group')
        segments = group['segments']
        if not segments:
            raise PlatformPause('empty detail group')
        keys = [image_key(s['src']) for s in segments]
        if len(segments) > 1:
            offset, original = 0, None
            for segment in segments:
                crop = re.search(r'~crop,(\d+),(\d+),(\d+),(\d+)~', segment['src'])
                if not crop:
                    raise PlatformPause('split module geometry not readable')
                x, y, width, height = map(int, crop.groups())
                stem = re.sub(r'~crop,[^~]+~', '', image_key(segment['src']))
                if x != 0 or y != offset or width != segment['width'] or height != segment['height']:
                    raise PlatformPause('split modules missing or reversed')
                if original is not None and stem != original:
                    raise PlatformPause('split modules belong to different images')
                original, offset = stem, offset + height
        mapping[name] = keys
        current_names.append(name)
        current_keys.extend(keys)
    if set(mapping) != set(expected) or len(current_keys) != audit['module_count'] or len(set(current_keys)) != len(current_keys):
        raise PlatformPause('detail coverage incomplete or repeated modules')
    return {'current_names': current_names, 'current_keys': current_keys,
            'desired_keys': [k for name in expected for k in mapping[name]],
            'expected_names': expected, 'content_accepted': False,
            'identity_basis': 'unique content correspondence for authorized sorting only; not filename/version acceptance'}
