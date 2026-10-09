from __future__ import annotations

from collections import Counter
from decimal import Decimal
import re
import time
import base64
import hashlib
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from PIL import Image, ImageChops, ImageStat

from .browser import PlatformPause, LocatorError, safe_url
from .workflow import Observation
from .media import same_pixels, verify_segments, ensure_version


def sku_completeness(expected, actual, declared_count):
    def key(row):
        return tuple(row.get("combination", [row.get("name")]))

    wanted, found = [key(r) for r in expected], [key(r) for r in actual]
    columns = {"name", "price", "inventory", "area", "cable"}
    duplicates = [list(k) for k, n in Counter(found).items() if n > 1]
    missing = [list(k) for k in wanted if k not in found]
    extra = [list(k) for k in found if k not in wanted]
    incomplete = [list(key(r)) for r in actual if not columns <= r.keys() or
                  any(r.get(k) in (None, "") for k in columns)]
    ok = (type(declared_count) is int and declared_count == len(actual) == len(expected)
          and not duplicates and not missing and not extra and not incomplete
          and len(set(wanted)) == len(wanted))
    return {"ok": ok, "declared_count": declared_count, "read_count": len(actual),
            "duplicates": duplicates, "missing": missing, "extra": extra, "incomplete": incomplete}


class Qianniu:
    @property
    def live(self):
        return self.browser.source == "live_playwright"

    def __init__(self, browser, profile, store):
        self.browser, self.profile, self.store = browser, profile, store
        self.product = None
        self.blockers = []

    def capture(self, label):
        return self.browser.capture(label)

    def run_phase(self, product, action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except BaseException as error:
            state = 'submission_unknown' if self.store.intent(product.task_id) else 'paused'
            self.store.state(product.task_id, state, {'phase': action.__name__, 'reason': str(error), 'capture': 'pending'})
            try:
                evidence = self.capture('phase-paused')
            except Exception as capture_error:
                evidence = {'capture_error': str(capture_error)}
            self.store.event(product.task_id, 'phase_failure', {'phase': action.__name__, 'reason': str(error), 'evidence': evidence})
            raise

    def ensure_form(self, product):
        identity = self._identity(product, require_category=False)
        if identity.get("category") == product.value("category"):
            return {**identity, "navigated": False, "source": self.browser.source}
        config = self.profile.get("entry")
        if not config or urlsplit(self.browser.page.url).path != config["category_path"]:
            raise PlatformPause("do not replace an existing form or unknown page; confirmed category entry required")
        if self.store and self.store.intent(product.task_id):
            raise PlatformPause("submission already reserved; navigate only for reconciliation")
        if "category" not in product.known_paths():
            raise PlatformPause("confirmed category evidence required before navigation")
        if product.data.get('media', {}).get('input_mode') == 'local_folder':
            return self.start_local_entry(product)
        url = config["publish_url"].format(category=product.value("category"))
        self.browser._host(url)
        if self.store:
            self.store.event(product.task_id, "form_entry_intent", {"category": product.value("category"), "url": safe_url(url)})
        self.browser.goto(url)
        self.browser.wait(lambda: self._identity(product), timeout=15)
        result = {**self._identity(product), "navigated": True, "source": self.browser.source,
                  "evidence": self.capture("form-entry")}
        if self.store:
            self.store.event(product.task_id, "form_entry", result)
        return result

    def start_local_entry(self, product):
        from .local_images import upload_plan

        self._identity(product, require_category=False)
        entry = self.profile.get('entry') or {}
        if urlsplit(self.browser.page.url).path != entry.get('category_path'):
            raise PlatformPause('local upload requires the image publishing entry; preserve existing forms')
        if not self.store or self.store.intent(product.task_id):
            raise PlatformPause('persistent records required and submission intent must be absent')
        plan = upload_plan(product)
        self.store.event(product.task_id, 'local_image_plan', plan)
        if not plan['group_ready']['main'] or 'category' not in product.known_paths():
            raise PlatformPause('local main image plan or confirmed category incomplete')
        config = entry.get('local_upload') or {}
        if config.get('verified') is not True:
            raise PlatformPause('image publishing upload/transition bindings not live verified; no files transferred')
        if any(e['kind'] == 'entry_upload_intent' for e in self.store.report(product.task_id)['events']):
            raise PlatformPause('entry upload result unknown or already dispatched; inspect actual page, never upload twice')
        if not config.get('previews') or not config.get('empty') or not config.get('open'):
            raise PlatformPause('entry empty-state and preview guards required')

        def guard_entry():
            self._identity(product, require_category=False)
            if urlsplit(self.browser.page.url).path != entry['category_path']:
                raise PlatformPause('entry changed before image transfer')
            if self.browser.resolve(config['previews'], missing_ok=True) is not None:
                raise PlatformPause('entry already contains images; preserve and inspect them')
            self.browser.resolve(config['empty'])
            ensure_version(product)

        guard_entry()
        evidence = self.capture('entry-upload-before')

        def reserve():
            guard_entry()
            self.store.event(product.task_id, 'entry_upload_intent', {
                'version': product.version, 'main': plan['groups']['main'],
                'authorization': plan['authorization'], 'evidence': evidence, 'platform_saved': False})

        self.browser.choose_files(config['open'], [r['path'] for r in plan['groups']['main']],
                                  before_action=reserve, before_transfer=guard_entry)
        self.store.event(product.task_id, 'entry_upload_dispatched', {
            'version': product.version, 'platform_saved': False, 'evidence': self.capture('entry-upload-dispatched')})

        def entered():
            url = urlsplit(self.browser.page.url)
            if url.path == entry['category_path']:
                return False
            if url.path != urlsplit(entry.get('publish_url', '')).path:
                raise PlatformPause('unexpected page after upload; no category guess or automatic navigation')
            return self._identity(product)

        self.browser.wait(entered, timeout=config.get('timeout', 60))
        result = {**self._identity(product), 'navigated': True, 'source': self.browser.source,
                  'platform_saved': False, 'images_verified': False, 'submitted': False,
                  'evidence': self.capture('local-entry-form')}
        self.store.event(product.task_id, 'local_entry_form', result)
        return result

    def open_panel(self, product, name):
        if name not in ("specification", "materials", "logistics"):
            raise PlatformPause("only non-submitting inspection panels are allowed")
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause("existing submission intent permits reconciliation only")
        config = self.profile.get("panels", {}).get(name)
        if not config:
            raise PlatformPause("panel opener not observed")
        for selector in config.get('conflicts', []):
            existing = self.browser.resolve(selector, missing_ok=True)
            if existing is not None and existing.is_checked():
                raise PlatformPause('conflicting origin mode; preserve current selection')
        if config.get("empty_region"):
            text = self.browser.resolve(config["empty_region"]).inner_text().strip()
            if text != config["empty_text"]:
                raise PlatformPause("SKU region is not the observed empty state; inspect all existing rows first")
        self.store.event(product.task_id, "panel_inspection_intent", {"panel": name, "version": product.version})
        self.browser.act(config["selector"], "click")
        for selector in config.get('after', []):
            self.browser.wait(lambda: self.browser.resolve(selector, missing_ok=True))
            self.browser.act(selector, selector.get('action', 'check'))
        result = {"state": "panel_opened", "panel": name, "source": self.browser.source,
                  "evidence": self.capture("panel-" + name), "committed": False}
        self.store.event(product.task_id, "panel_inspection", result)
        return result

    def close_panel(self, product, name):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause("existing submission intent permits reconciliation only")
        config = self.profile.get("panels", {}).get(name) or {}
        if not config.get("region") or not config.get("close"):
            raise PlatformPause("panel cancel control not observed")
        events = self.store.report(product.task_id)["events"]
        import json
        history = [e for e in events if e["kind"] in ("panel_inspection", "panel_closed")
                   and json.loads(e["payload"]).get("panel") == name]
        if not history or history[-1]["kind"] != "panel_inspection":
            raise PlatformPause("only the program's inspection panel may be cancelled")
        panel = self.browser.resolve(config["region"])
        controls = panel.locator('input:not([type=hidden]):not([type=radio]):not([type=checkbox]),textarea')
        if any(c.is_visible() and c.input_value().strip() for c in controls.all()):
            raise PlatformPause("panel contains staged text; preserve user changes")
        if config.get('staged_checkboxes') and any(c.is_checked() for c in panel.locator(config['staged_checkboxes']).all()):
            raise PlatformPause('panel has staged selections; preserve user choices')
        self.store.event(product.task_id, "panel_close_intent", {"panel": name, "version": product.version})
        self.browser.act(config["close"], "click")
        self.browser.wait(lambda: self.browser.resolve(config["region"], missing_ok=True) is None)
        result = {"state": "panel_closed", "panel": name, "source": self.browser.source,
                  "evidence": self.capture("panel-closed-" + name), "committed": False}
        self.store.event(product.task_id, "panel_closed", result)
        return result

    def _origin_value(self):
        config = self.profile['origin']
        target = self.browser.resolve(config['value'])
        value = target.get_attribute(config['value_attribute']) if config.get('value_attribute') else self._value(target)
        if not value:
            raise PlatformPause('origin display value unavailable')
        return value.strip()

    def set_origin(self, product, observed_current):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; origin mutation forbidden')
        if not {'logistics.province', 'logistics.city'} <= set(product.known_paths()):
            raise PlatformPause('confirmed province/city sources required')
        config = self.profile.get('origin') or {}
        province = config.get('province_names', {}).get(product.value('logistics.province'))
        city = config.get('city_names', {}).get(product.value('logistics.city'))
        if not province or not city:
            raise PlatformPause('configured origin labels not observed')
        expected = province + ' / ' + city
        actual = self._origin_value()
        if actual != expected:
            if not observed_current or actual != observed_current:
                raise PlatformPause('origin changed from the inspected baseline; preserve current address')
            ensure_version(product)
            self.store.event(product.task_id, 'origin_intent', {'before': actual, 'desired': expected,
                'version': product.version, 'source': self.browser.source, 'evidence': self.capture('origin-before')})
            if self.browser.resolve(config['options'], missing_ok=True) is None:
                self.browser.act(config['control'], 'click')
            for name in (province, city):
                option = {'scope': [config['options']], 'role': 'option', 'name': name}
                if config.get('option_css'):
                    import json
                    option = {'scope': [config['options']], 'css': config['option_css'].replace('{name}', json.dumps(name, ensure_ascii=False))}
                self.browser.wait(lambda: self.browser.resolve(option, missing_ok=True))
                self.browser.act(option, 'click')
            self.browser.wait(lambda: self._origin_value() == expected)
        actual = self._origin_value()
        result = {'state': 'origin_readback', 'actual': actual, 'province': product.value('logistics.province'),
                  'city': product.value('logistics.city'), 'submitted': False, 'source': self.browser.source,
                  'evidence': self.capture('origin-readback')}
        self.store.event(product.task_id, 'origin_readback', result)
        return result

    def _detail_modules(self):
        from .detail_sort import image_key
        config = self.profile['detail_sort']
        container = self.browser.resolve(config['container'])
        modules = []
        for tile in container.locator(config['items']).all():
            images = tile.locator(config['image'])
            if images.count() != 1 or tile.get_attribute('draggable') != 'true':
                raise PlatformPause('detail tile has no unique image/draggable control')
            src = images.get_attribute('src')
            if not src:
                raise PlatformPause('detail thumbnail source not readable')
            modules.append({'key': image_key(src), 'src': src})
        keys = [m['key'] for m in modules]
        if not keys or len(set(keys)) != len(keys):
            raise PlatformPause('missing or duplicate detail modules')
        return modules

    @staticmethod
    def _detail_signature(audit):
        return [[s['src'], s['sha256']] for g in audit['groups'] for s in g['segments']]

    def ack_manual_details(self, product, statement):
        if not statement or not statement.strip():
            raise PlatformPause('direct user statement required for manual handoff')
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; reconciliation only')
        ensure_version(product)
        audit = self.audit_detail_preview(product)
        result = {'state': 'manual_detail_order_recorded', 'statement': statement,
                  'skip_automatic_sort': True, 'content_accepted': False, 'submitted': False,
                  'version': product.version, 'signature': self._detail_signature(audit),
                  'audit': audit, 'source': self.browser.source}
        self.store.event(product.task_id, 'manual_detail_order', result)
        return result

    def _manual_details_current(self, product, audit):
        import json
        if not self.store:
            return False
        records = [e for e in self.store.report(product.task_id)['events'] if e['kind'] == 'manual_detail_order']
        if not records:
            return False
        record = json.loads(records[-1]['payload'])
        return (record['version'] == product.version and record['skip_automatic_sort'] is True
                and record['signature'] == self._detail_signature(audit))

    def stage_single_sku(self, product):
        self._identity(product)
        if self.store.intent(product.task_id) or len(product.data['skus']) != 1:
            raise PlatformPause('staging requires one SKU and no submission intent')
        config = self.profile.get('panels', {}).get('specification') or {}
        if not config.get('custom_mode') or not config.get('empty_region'):
            raise PlatformPause('single-SKU mode binding not observed')
        self.browser.resolve(config['region'])
        if self.browser.resolve(config['empty_region']).inner_text().strip() != config['empty_text']:
            raise PlatformPause('existing specifications must be read before staging')
        ensure_version(product)
        self.store.event(product.task_id, 'sku_mode_intent', {'name': product.value('skus.0.name'), 'version': product.version})
        target = self.browser.resolve(config['custom_mode'])
        if not target.is_checked():
            self.browser.act(config['custom_mode'], 'check')
        result = {'state': 'sku_mode_selected', 'committed': False, 'source': self.browser.source,
                  'name': product.value('skus.0.name')}
        if config.get('name'):
            if 'skus.0.name' not in product.known_paths():
                raise PlatformPause('confirmed SKU name required')
            panel = self.browser.resolve(config['region'])
            self.browser.wait(lambda: panel.locator(config['name_inputs']).count() > 0, timeout=10)
            values = [c.input_value().strip() for c in panel.locator(config['name_inputs']).all() if c.input_value().strip()]
            if values not in ([], [product.value('skus.0.name')]):
                raise PlatformPause('staged SKU conflict; preserve all current names')
            if not values:
                self.browser.act(config['name'], 'fill', product.value('skus.0.name'))
            actual = [c.input_value().strip() for c in panel.locator(config['name_inputs']).all() if c.input_value().strip()]
            if actual != [product.value('skus.0.name')]:
                raise PlatformPause('single staged SKU readback differs')
            result.update(state='sku_name_staged', actual=actual)
        result['evidence'] = self.capture('sku-custom-mode')
        self.store.event(product.task_id, 'sku_mode_readback', result)
        return result

    def open_sku_images(self, product):
        import json
        self._identity(product)
        if self.store.intent(product.task_id) or len(product.data['skus']) != 1:
            raise PlatformPause('single-SKU image picker requires no submission intent')
        config = self.profile.get('panels', {}).get('specification') or {}
        if not config.get('material_open'):
            raise PlatformPause('SKU image slot binding not observed')
        panel = self.browser.resolve(config['region'])
        actual = [c.input_value().strip() for c in panel.locator(config['name_inputs']).all() if c.input_value().strip()]
        if actual != [product.value('skus.0.name')]:
            raise PlatformPause('image slot does not belong to the configured single SKU')
        selector = dict(config['material_open'])
        selector['css'] = selector['css'].replace('{name}', json.dumps(product.value('skus.0.name')))
        ensure_version(product)
        self.store.event(product.task_id, 'sku_picker_intent', {'sku': actual, 'version': product.version})
        self.browser.act(selector, 'click')
        result = {'state': 'sku_image_picker_opened', 'source': self.browser.source,
                  'selected': False, 'evidence': self.capture('sku-image-picker')}
        self.store.event(product.task_id, 'sku_picker_opened', result)
        return result

    def browse_sku_materials(self, product, kind='sku'):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; reconciliation only')
        config = self.profile.get('sku_materials') or {}
        if not config.get('tree'):
            raise PlatformPause('material folder controls not observed')
        folder = product.data['media']['folders'][kind]
        parts = folder.split('/')
        if len(parts) != 2:
            raise PlatformPause('observed material folder adapter requires two levels')
        tree = self.browser.resolve(config['tree'])
        items = tree.locator('[role=treeitem]').evaluate_all('''es => es.map(e => ({
            name:e.innerText.trim(), level:Number(e.getAttribute('aria-level')),
            expanded:e.getAttribute('aria-expanded'), id:e.closest('li').getAttribute('value')}))''')
        parents = [i for i, item in enumerate(items) if item['name'] == parts[0] and item['level'] == 1]
        children = [i for i, item in enumerate(items) if item['name'] == parts[1] and item['level'] == 2]
        if len(parents) != 1 or len(children) != 1:
            raise PlatformPause('material folder hierarchy is not unique')
        parent, child = parents[0], children[0]
        preceding = [i for i in range(child) if items[i]['level'] == 1]
        if not preceding or preceding[-1] != parent or items[parent]['expanded'] != 'true':
            raise PlatformPause('material folder hierarchy not established; preserve current picker')
        if not items[parent]['id'] or not items[child]['id']:
            raise PlatformPause('platform folder identifiers missing')
        selector = {**config['tree'], 'scope': config['tree'].get('scope', [])}
        scope = {k: v for k, v in selector.items() if k != 'scope'}
        target = {'scope': [scope], 'role': 'treeitem', 'name': parts[1]}
        if 'frame_url' in selector:
            target['frame_url'] = selector['frame_url']
        def card_ids():
            container = self.browser.resolve(config['cards'], missing_ok=True)
            if container is None:
                return []
            return sorted(container.locator(config['checkbox']).evaluate_all('(es)=>es.map(e=>e.getAttribute("value"))'))
        previous_ids = card_ids()
        already_selected = self.browser.resolve(target).get_attribute('aria-selected') == 'true'
        if config.get('list_transition') and already_selected:
            raise PlatformPause('folder already selected; no fresh list transition proof, preserve picker')
        ensure_version(product)
        self.store.event(product.task_id, 'material_folder_intent', {'folder': folder, 'folder_ids': [items[parent]['id'], items[child]['id']]})
        self.browser.act(target, 'click')
        self.browser.wait(lambda: self.browser.resolve(target).get_attribute('aria-selected') == 'true')
        if config.get('list_folder') and config.get('list_folder_attribute'):
            self.browser.wait(lambda: self.browser.resolve(config['list_folder']).get_attribute(config['list_folder_attribute']) == items[child]['id'])
        elif config.get('list_transition'):
            self.browser.wait(lambda: bool(card_ids()) and card_ids() != previous_ids, timeout=12)
        else:
            raise PlatformPause('material list folder identity binding not observed; selection is not loading proof')
        local_name = Path(product.value('skus.0.image')).name
        def loaded():
            container = self.browser.resolve(config['cards'], missing_ok=True)
            return container is not None and any(c.locator(config['filename']).inner_text().strip() == local_name
                for c in container.locator(config.get('card_items', ':scope')).all())
        if kind == 'sku':
            self.browser.wait(loaded, timeout=12)
        # Cards are a collection; the resolver still enforces uniqueness of their container.
        container = self.browser.resolve(config['cards'])
        cards = container.locator(config.get('card_items', ':scope'))
        materials = []
        for card in cards.all():
            image, checkbox = card.locator(config['image']), card.locator(config['checkbox'])
            if image.count() != 1 or checkbox.count() != 1:
                raise PlatformPause('material card metadata not unique')
            materials.append({'filename': card.locator(config['filename']).inner_text().strip(),
                              'dimensions': card.locator(config['dimensions']).inner_text().strip(),
                              'material_id': checkbox.get_attribute('value'), 'preview': image.get_attribute('src')})
        result = {'state': 'sku_material_folder_read', 'folder': folder,
                  'folder_ids': [items[parent]['id'], items[child]['id']], 'materials': materials,
                  'coverage': 'visible_cards_only', 'selected': False, 'source': self.browser.source,
                  'list_transition': {'before_ids': previous_ids, 'after_ids': card_ids(), 'kind': kind},
                  'evidence': self.capture('sku-material-folder')}
        self.store.event(product.task_id, 'sku_material_folder', result)
        return result

    def _upload_authorization(self, product, relative, folder):
        records = [r for r in product.data['media'].get('local_upload_authorizations', [])
                   if r.get('file') == relative and r.get('folder') == folder]
        if (len(records) != 1 or records[0].get('confirmed') is not True or not records[0].get('source')
                or folder not in product.data['media']['folders'].values()
                or relative not in product.data['media'].get('detail', []) + product.data['media'].get('main', [])
                   + [product.value(f'skus.{i}.image') for i in range(len(product.data['skus']))]):
            raise PlatformPause('exact file/folder upload authorization with source required')
        return records[0]

    def open_local_upload(self, product, relative, folder):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; upload forbidden')
        authorization = self._upload_authorization(product, relative, folder)
        ensure_version(product)
        config = self.profile.get('local_upload') or {}
        if not config.get('open'):
            raise PlatformPause('local upload opener not observed')
        self.store.event(product.task_id, 'upload_panel_intent', {'file': relative, 'folder': folder,
                         'authorization': authorization, 'version': product.version})
        self.browser.act(config['open'], 'click')
        result = {'state': 'upload_panel_opened', 'file': relative, 'folder': folder, 'uploaded': False,
                  'submitted': False, 'source': self.browser.source, 'evidence': self.capture('upload-panel')}
        self.store.event(product.task_id, 'upload_panel', result)
        return result

    def open_upload_folder(self, product, relative, folder):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; upload forbidden')
        self._upload_authorization(product, relative, folder)
        self.browser.act(self.profile['local_upload']['destination'], 'click')
        result = {'state': 'upload_folder_options_opened', 'file': relative, 'folder': folder,
                  'uploaded': False, 'submitted': False, 'evidence': self.capture('upload-folder-options')}
        self.store.event(product.task_id, 'upload_folder_options', result)
        return result

    def _upload_folder_value(self):
        value = self.browser.resolve(self.profile['local_upload']['destination']).get_attribute('aria-valuetext')
        if not value:
            raise PlatformPause('full upload destination unreadable')
        return '/'.join(part.strip() for part in value.split('/'))

    def set_upload_folder(self, product, relative, folder):
        import json
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; upload forbidden')
        self._upload_authorization(product, relative, folder)
        config = self.profile['local_upload']
        ensure_version(product)
        if self._upload_folder_value() != folder:
            if self.browser.resolve(config['options'], missing_ok=True) is None:
                self.open_upload_folder(product, relative, folder)
            for part in folder.split('/'):
                selector = {'frame_url': config['options'].get('frame_url'), 'scope': [config['options']],
                            'css': config['option_css'].replace('{name}', json.dumps(part, ensure_ascii=False))}
                self.browser.wait(lambda: self.browser.resolve(selector, missing_ok=True))
                self.browser.act(selector, 'click')
            self.browser.wait(lambda: self._upload_folder_value() == folder)
        result = {'state': 'upload_destination_readback', 'actual_folder': self._upload_folder_value(),
                  'uploaded': False, 'submitted': False, 'evidence': self.capture('upload-destination')}
        self.store.event(product.task_id, 'upload_destination', result)
        return result

    def upload_local_material(self, product, relative, folder):
        import json
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; upload forbidden')
        authorization = self._upload_authorization(product, relative, folder)
        target_name = Path(relative).name.casefold()
        prior = [e for e in self.store.report(product.task_id)['events'] if e['kind'] == 'local_upload_intent'
                 and Path(json.loads(e['payload']).get('file', '')).name.casefold() == target_name
                 and json.loads(e['payload']).get('folder', '').casefold() == folder.casefold()]
        if prior:
            raise PlatformPause('upload result unknown or already dispatched; inspect material center, never transfer again')
        listings = [json.loads(e['payload']) for e in self.store.report(product.task_id)['events']
                    if e['kind'] == 'sku_material_folder' and json.loads(e['payload']).get('folder') == folder]
        if listings and any(m.get('filename', '').casefold() == target_name for m in listings[-1].get('materials', [])):
            raise PlatformPause('authorized material already exists; read full content instead of uploading a duplicate')
        config = self.profile['local_upload']
        if self._upload_folder_value() != folder or not self.browser.resolve(config['original']).is_checked():
            raise PlatformPause('exact upload folder and original-image mode required before transfer')
        root = (product.base / product.data['media']['root']).resolve()
        local = (root / relative).resolve()
        if not local.is_relative_to(root) or not local.is_file():
            raise PlatformPause('authorized local image missing or escapes product root')
        if local.stat().st_size > 20 * 1024 * 1024:
            raise PlatformPause('image exceeds observed upload limit')
        with Image.open(local) as im:
            if min(im.size) < 200:
                raise PlatformPause('image below observed upload dimensions')
            size = list(im.size)
        files = self.browser.resolve(config['files'], visible=False)
        if files.inner_text().strip() or files.locator('img').count():
            raise PlatformPause('upload panel already has staged files; preserve and inspect')
        target = self.browser.resolve(config['input'], visible=False)
        if not target.is_enabled() or not target.evaluate("e=>e.tagName==='INPUT' && e.type==='file'"):
            raise PlatformPause('unique enabled file input required before intent')
        ensure_version(product)
        local_sha = hashlib.sha256(local.read_bytes()).hexdigest()
        def before_transfer():
            self._identity(product)
            ensure_version(product)
            self._upload_authorization(product, relative, folder)
            if self._upload_folder_value() != folder or not self.browser.resolve(config['original']).is_checked():
                raise PlatformPause('upload destination/original mode changed before transfer')
            staged = self.browser.resolve(config['files'], visible=False)
            if staged.inner_text().strip() or staged.locator('img').count():
                raise PlatformPause('staged upload files changed before transfer')
            if hashlib.sha256(local.read_bytes()).hexdigest() != local_sha:
                raise PlatformPause('local file changed before transfer')
        self.store.event(product.task_id, 'local_upload_intent', {'file': relative, 'folder': folder,
                         'version': product.version, 'sha256': local_sha, 'local_path': str(local),
                         'size': size, 'authorization': authorization, 'evidence': self.capture('upload-before')})
        self.browser.act(config['input'], 'files', str(local), before_action=before_transfer)
        self.store.event(product.task_id, 'local_upload_dispatched', {'file': relative, 'folder': folder,
                         'complete': False, 'evidence': self.capture('upload-dispatched')})
        if not config.get('complete_text'):
            raise PlatformPause('upload result unknown; observe actual progress before accepting completion')
        self.browser.wait(lambda: config['complete_text'] in self.browser.resolve(config['files']).inner_text(), timeout=60)
        raise PlatformPause('upload progress completed; material ID/full content readback still required')

    def _sku_card_selector(self, product):
        import json
        config = self.profile['sku_materials']
        container = config['cards']
        return {'frame_url': container.get('frame_url'), 'scope': [container],
                'css': config['card_items'] + ':has(' + config['filename'] + ':text-is(' +
                       json.dumps(Path(product.value('skus.0.image')).name, ensure_ascii=False) + '))'}

    def open_sku_preview(self, product):
        self._identity(product)
        if self.store.intent(product.task_id) or len(product.data['skus']) != 1:
            raise PlatformPause('single-SKU preview requires no submission intent')
        ensure_version(product)
        config = self.profile['sku_materials']
        selector = self._sku_card_selector(product)
        card = self.browser.resolve(selector)
        checkbox = card.locator(config['checkbox'])
        if checkbox.count() != 1 or checkbox.is_checked():
            raise PlatformPause('preview requires a unique unselected material')
        material_id = checkbox.get_attribute('value')
        if not material_id:
            raise PlatformPause('material identifier missing')
        self.browser.act(selector, 'hover')
        preview = {**selector, 'scope': selector['scope'] + [{'css': selector['css']}], 'css': config['full_view']}
        self.browser.act(preview, 'click')
        result = {'state': 'sku_material_preview_opened', 'name': Path(product.value('skus.0.image')).name,
                  'material_id': material_id, 'selected': False, 'source': self.browser.source,
                  'evidence': self.capture('sku-material-preview')}
        self.store.event(product.task_id, 'sku_material_preview', result)
        return result

    def commit_single_sku_name(self, product):
        import json
        self._identity(product)
        if self.store.intent(product.task_id) or len(product.data['skus']) != 1:
            raise PlatformPause('name-only creation requires one SKU and no submission intent')
        if 'skus.0.name' not in product.known_paths():
            raise PlatformPause('confirmed SKU name with source required')
        name = product.value('skus.0.name')
        prior = [e for e in self.store.report(product.task_id)['events'] if e['kind'] == 'sku_name_create_intent']
        if prior:
            try:
                before = self.read_all_skus()
            except Exception as error:
                raise PlatformPause('creation result unknown; full SKU readback unavailable, never confirm again') from error
            if before['declared_count'] == 1 and [r.get('name') for r in before['rows']] == [name]:
                result = {'state': 'sku_name_created_image_pending', 'actual_names': [r['name'] for r in before['rows']],
                          'readback': before, 'image_accepted': False, 'submitted': False, 'reconciled': True,
                          'source': self.browser.source}
                self.store.event(product.task_id, 'sku_name_create_readback', result)
                return result
            raise PlatformPause('creation result unknown; preserve drawer and reconcile, never confirm again')
        config = self.profile['panels']['specification']
        media = self.profile.get('sku_materials') or {}
        if media.get('preview_image'):
            picture = self.browser.resolve(media['preview_image'], missing_ok=True)
            if picture is not None:
                card = self.browser.resolve(self._sku_card_selector(product))
                src = picture.get_attribute('src')
                thumbnail = card.locator(media['image']).get_attribute('src')
                if not src or not thumbnail or not urlsplit(thumbnail).path.startswith(urlsplit(src).path + '_'):
                    raise PlatformPause('full preview does not belong to the configured material')
                payload = self._image_bytes(src)
                root = (product.base / product.data['media']['root']).resolve()
                local = (root / product.value('skus.0.image')).resolve()
                if not local.is_relative_to(root):
                    raise PlatformPause('SKU image escapes product root')
                with Image.open(BytesIO(payload)) as remote, Image.open(local) as original:
                    proof = {'name': local.name, 'material_id': card.locator(media['checkbox']).get_attribute('value'),
                             'src': src, 'remote_size': list(remote.size), 'local_size': list(original.size),
                             'remote_format': remote.format, 'local_format': original.format,
                             'content_ok': same_pixels(payload, local), 'source': self.browser.source,
                             'remote_sha256': hashlib.sha256(payload).hexdigest(),
                             'local_sha256': hashlib.sha256(local.read_bytes()).hexdigest(), 'accepted': False}
                image_path = self.browser.evidence_dir / ('sku-full-preview-' + proof['remote_sha256'][:12] + '.image')
                image_path.write_bytes(payload)
                proof['image_file'] = str(image_path.resolve())
                self.store.event(product.task_id, 'sku_material_content_pending', proof)
                self.browser.act(media['preview_close'], 'click')
                self.browser.wait(lambda: self.browser.resolve(media['preview_image'], missing_ok=True) is None)
        if config.get('image_dialog'):
            dialog = self.browser.resolve(config['image_dialog'], missing_ok=True)
            if dialog is not None:
                if dialog.locator(config['image_dialog_images']).count():
                    raise PlatformPause('image dialog has staged images; preserve for verification')
                self.browser.act(config['image_dialog_cancel'], 'click')
                self.browser.wait(lambda: self.browser.resolve(config['image_dialog'], missing_ok=True) is None)
        panel = self.browser.resolve(config['region'])
        actual = [c.input_value().strip() for c in panel.locator(config['name_inputs']).all() if c.input_value().strip()]
        name = product.value('skus.0.name')
        if actual != [name] or panel.locator(config['staged_images']).count():
            raise PlatformPause('name-only creation cannot accept conflicts or unverified staged images')
        if self.browser.resolve(config['empty_region']).inner_text().strip() != config['empty_text']:
            raise PlatformPause('existing SKU table changed; reread all rows')
        ensure_version(product)
        self.store.event(product.task_id, 'sku_name_create_intent', {'name': name, 'version': product.version,
                         'image_pending_reason': 'platform conversion/content verification pending'})
        self.browser.act(config['confirm'], 'click')
        self.browser.wait(lambda: self.browser.resolve(config['region'], missing_ok=True) is None)
        self.browser.wait(lambda: self.browser.resolve(config['empty_region']).get_by_text(name, exact=True).count() == 1)
        after = self.read_all_skus()
        actual_names = [r.get('name') for r in after['rows']]
        self.store.event(product.task_id, 'sku_name_actual_rows', after)
        if after['declared_count'] != 1 or actual_names != [name]:
            raise PlatformPause('actual SKU set/count differs after creation; no further creation')
        result = {'state': 'sku_name_created_image_pending', 'actual_names': actual_names, 'readback': after, 'image_accepted': False,
                  'submitted': False, 'source': self.browser.source, 'evidence': self.capture('sku-name-created')}
        self.store.event(product.task_id, 'sku_name_create_readback', result)
        return result

    def _detail_preview_keys(self):
        from .detail_sort import image_key
        config = self.profile['detail_preview']
        frames = [f for f in self.browser.page.frames if re.search(config['frame_url'], f.url)]
        if len(frames) != 1:
            raise PlatformPause('detail preview frame not unique')
        return [image_key(s) for s in frames[0].locator(config['images']).evaluate_all('(es)=>es.map(e=>e.src)')]

    def reorder_details(self, product, authorization):
        from .detail_sort import sort_plan, image_key
        import json
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; preserve form for reconciliation')
        handoffs = [e for e in self.store.report(product.task_id)['events'] if e['kind'] == 'manual_detail_order']
        if handoffs and json.loads(handoffs[-1]['payload']).get('skip_automatic_sort') is True:
            raise PlatformPause('manual detail order handoff forbids automatic sorting')
        if not self.profile.get('detail_sort') or not self.profile.get('detail_preview'):
            raise PlatformPause('detail sorting controls not observed')
        ensure_version(product)
        before = self.audit_detail_preview(product)
        plan = sort_plan(before, authorization)
        modules = self._detail_modules()
        if [m['key'] for m in modules] != plan['current_keys']:
            raise PlatformPause('editor and preview detail modules differ')
        self.store.event(product.task_id, 'detail_sort_authorization',
                         {'authorization': authorization, 'version': product.version, 'scope': 'order only, no upload/replace/submit',
                          'before': before, 'plan': plan, 'evidence': self.capture('detail-sort-before')})
        moves, desired = 0, plan['desired_keys']
        try:
            for index, key in enumerate(desired):
                self._identity(product)
                ensure_version(product)
                current = self._detail_modules()
                keys = [m['key'] for m in current]
                if Counter(keys) != Counter(desired) or keys[:index] != desired[:index]:
                    raise PlatformPause('detail modules changed during sorting')
                if keys[index] == key:
                    continue
                source = next(m for m in current if m['key'] == key)
                destination = current[index]
                config = self.profile['detail_sort']
                selector = lambda m: {'scope': [config['container']],
                    'css': config['items'] + ':has(' + config['image'] + '[src=' + json.dumps(m['src']) + '])',
                    'drag_handle': config.get('handle')}
                expected = keys.copy()
                expected.remove(key)
                expected.insert(index, key)
                self.store.event(product.task_id, 'detail_move_intent', {'source': source, 'before': destination,
                                 'expected': expected, 'version': product.version})
                self.browser.drag_before(selector(source), selector(destination))
                self.browser.wait(lambda: [m['key'] for m in self._detail_modules()] == expected, timeout=5)
                self.browser.wait(lambda: self._detail_preview_keys() == expected, timeout=8)
                moves += 1
                self.store.event(product.task_id, 'detail_move_readback', {'actual': self._detail_preview_keys(), 'move': moves})
            after = self.audit_detail_preview(product)
            after_plan = sort_plan(after, authorization)
            signatures = lambda a: sorted((image_key(s['src']), s['sha256']) for g in a['groups'] for s in g['segments'])
            if after_plan['current_keys'] != desired or signatures(before) != signatures(after):
                raise PlatformPause('final detail order/content changed unexpectedly')
            ensure_version(product)
            result = {'state': 'detail_order_verified', 'order_ok': True, 'moves': moves,
                      'module_count': len(desired), 'actual_order': after_plan['current_names'],
                      'all_existing_segments_preserved': True, 'content_accepted': False, 'submitted': False,
                      'source': self.browser.source, 'version': product.version,
                      'audit': after, 'evidence': self.capture('detail-sort-after')}
            self.store.event(product.task_id, 'detail_order_verified', result)
            self.store.state(product.task_id, 'paused', {'reason': 'order corrected only; full material/SKU/logistics acceptance pending'})
            return result
        except Exception as e:
            self.store.state(product.task_id, 'paused', {'reason': str(e), 'scope': 'order only',
                             'evidence': self.capture('detail-sort-paused')})
            raise

    def _shop_matches(self, product):
        entry = self.profile.get('warehouse_entry') or {}
        if entry.get('shop') and re.search(entry.get('page_url_pattern', r'(?!)'), self.browser.page.url):
            shop = self.browser.resolve(entry['shop'], missing_ok=True)
            return [shop] if shop and shop.inner_text().strip() == product.value('shop') else []
        return [e for f in self.browser.page.frames
                   for e in f.get_by_text(product.value("shop") or "[missing shop]", exact=True).all()
                   if e.is_visible()]

    def _identity(self, product, require_category=True):
        self.browser.guard()
        matches = self._shop_matches(product)
        if len(matches) != 1:
            raise PlatformPause("expected shop identity not visible")
        category = parse_qs(urlsplit(self.browser.page.url).query).get("catId", [])
        if category != [str(product.value("category"))]:
            if require_category:
                raise PlatformPause("enter the confirmed product category form before filling")
            return {"shop": product.value("shop")}
        return {"shop": product.value("shop"), "category": category[0]}

    @staticmethod
    def _value(control):
        tag = control.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            return control.locator("option:checked").inner_text().strip()
        if tag in ("input", "textarea"):
            typ = control.get_attribute("type")
            if typ in ("radio", "checkbox"):
                if not control.is_checked():
                    return None
                return control.get_attribute("data-value") or control.get_attribute("value")
            return control.input_value().strip()
        return control.inner_text().strip()

    @staticmethod
    def _normalize(key, value):
        if value in (None, ""):
            return None
        if key.endswith("price"):
            price = Decimal(str(value))
            rounded = price.quantize(Decimal("0.01"))
            if not price.is_finite() or price != rounded:
                raise PlatformPause("actual price is not exact to cents")
            return str(rounded)
        if key.endswith(("inventory", "pack_quantity")) or key == "total_inventory":
            return int(value)
        return value

    def _read_binding(self, binding):
        target = self.browser.resolve(binding.get("value_selector", binding["selector"]))
        if binding.get("checked_value") is not None:
            value = binding["checked_value"] if target.is_checked() else None
        else:
            value = self._value(target)
        if binding.get('measurement_unit') and value not in (None, ''):
            value = self._measurement_number(value, binding['measurement_unit'])
        return binding.get("value_map", {}).get(value, value)

    def _business_units(self, product, row):
        result = dict(row)
        rules = [r for r in product.data.get('platform_unit_rules', []) if r.get('sku') == row.get('name')]
        if not rules:
            return result
        if len(rules) != 1:
            raise PlatformPause('unit mapping is duplicated for this SKU')
        rule = rules[0]
        matches = [i for i in range(len(product.data['skus'])) if product.value(f'skus.{i}.name') == row['name']]
        if len(matches) != 1:
            raise PlatformPause('unit mapping owner not unique')
        index = matches[0]
        if (rule.get('confirmed') is not True or not rule.get('source') or type(rule.get('factor')) is not int
                or rule['factor'] != 1 or rule.get('platform') != row.get('inventory_unit')
                or not {f'skus.{index}.sales_unit', f'skus.{index}.inventory_unit'} <= set(product.known_paths())
                or rule.get('business') != product.value(f'skus.{index}.inventory_unit')
                or rule.get('business') != product.value(f'skus.{index}.sales_unit')):
            raise PlatformPause('unit mapping lacks confirmed one-to-one evidence or differs from actual unit')
        result['platform_inventory_unit'] = row['inventory_unit']
        if 'sales_unit' in row:
            result['platform_sales_unit'] = row['sales_unit']
            if row['sales_unit'] not in (None, '', rule['platform'], rule['business']):
                raise PlatformPause('actual sales unit conflicts with confirmed unit mapping')
        result['inventory_unit'] = result['sales_unit'] = rule['business']
        result['unit_mapping_proof'] = dict(rule)
        return result

    @staticmethod
    def _measurement_number(value, unit):
        match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*' + re.escape(unit), value.strip())
        if not match:
            raise PlatformPause('measurement unit/value readback differs')
        return match.group(1)

    def _write_binding(self, binding, value):
        target = self.browser.resolve(binding["selector"])
        tag = target.evaluate("e => e.tagName.toLowerCase()")
        if binding.get('action') == 'measurement':
            if self.browser.resolve(binding['editor'], missing_ok=True) is None:
                self.browser.act(binding['selector'], 'click')
            self.browser.wait(lambda: self.browser.resolve(binding['editor'], missing_ok=True))
            if self._value(self.browser.resolve(binding['unit_selector'])) != binding['measurement_unit']:
                raise PlatformPause('measurement unit differs; do not infer numeric units')
            self.browser.act(binding['number_selector'], 'fill', value)
            self.browser.act(binding['finish'], 'click')
        elif tag == "select":
            self.browser.act(binding["selector"], "select", value)
        elif binding.get("action") == "choice":
            option = dict(binding["option"])
            option["name" if "role" in option else "text"] = str(value)
            if not self.browser.resolve(option, missing_ok=True):
                self.browser.act(binding["selector"], "click")
            self.browser.wait(lambda: self.browser.resolve(option, missing_ok=True), timeout=10)
            self.browser.act(option, "click")
        elif binding.get("action") == "check":
            self.browser.act(binding["selector"], "check")
        else:
            self.browser.act(binding["selector"], "fill", value)
        if self._read_binding(binding) != str(value):
            raise PlatformPause("actual choice differs from expected")

    def read_all_skus(self):
        config = self.profile.get("sku")
        if not config or not config.get("table"):
            raise PlatformPause("SKU table binding not yet verified")
        table = self.browser.resolve(config["table"])
        columns = config["columns"]
        header_controls = table.locator(config.get('headers', "thead th,[role=columnheader]"))
        headers = [(h.get_attribute(config['header_attribute']) or '').strip() for h in header_controls.all()] if config.get('header_attribute') else [s.strip() for s in header_controls.all_inner_texts()]
        if any(headers.count(name) != 1 for name in columns.values()):
            raise PlatformPause("SKU column header absent or ambiguous")
        indices = {key: headers.index(name) for key, name in columns.items()}
        rows = []
        for row in table.locator("tbody tr,[role=row]").all():
            cells = row.locator("td,[role=cell],[role=gridcell]")
            if not cells.count():
                continue
            result = {}
            for key, index in indices.items():
                cell = cells.nth(index)
                if key == "image":
                    if not self.product or not config.get("image"):
                        raise PlatformPause("per-SKU image content binding required")
                    name_cell = cells.nth(indices["name"])
                    sku_name = self._value(name_cell.locator("input")) if name_cell.locator("input").count() else name_cell.inner_text().strip()
                    desired = [i for i in range(len(self.product.data["skus"]))
                               if self.product.value(f"skus.{i}.name") == sku_name]
                    if len(desired) != 1:
                        raise PlatformPause("SKU image owner not unique")
                    relative = self.product.value(f"skus.{desired[0]}.image")
                    root = (self.product.base / self.product.data["media"]["root"]).resolve()
                    local = (root / relative).resolve()
                    if not local.is_relative_to(root):
                        raise PlatformPause("SKU image escapes root")
                    ensure_version(self.product)
                    proof = self.verify_material(cell, local, self.product.data["media"]["folders"]["sku"], config["image"])
                    if not proof["ok"]:
                        raise PlatformPause("SKU image content differs: " + sku_name)
                    result["image"], result["image_proof"] = relative, proof
                    continue
                if key in config.get('cell_values', {}):
                    controls = cell.locator(config['cell_values'][key])
                else:
                    controls = cell.locator(config.get('controls', "input:not([type=hidden]),textarea,select"))
                if controls.count() > 1:
                    raise PlatformPause("SKU cell has multiple editable controls")
                value = self._value(controls) if controls.count() else cell.inner_text().strip()
                if value and key in config.get('measurement_units', {}):
                    unit = config['measurement_units'][key]
                    value = self._measurement_number(value, unit)
                    result[key + '_unit'] = unit
                result[key] = self._normalize(key, value) if value else None
            rows.append(result)
        declared = None
        if config.get("count"):
            value = self.browser.resolve(config["count"]).inner_text()
            match = re.fullmatch(config["count_pattern"], value.strip())
            if match:
                declared = int(match.group(1))
        elif config.get("fully_rendered") is True and table.evaluate("e => e.tagName") == "TABLE":
            declared = len(rows)
        return {"rows": rows, "declared_count": declared, "evidence": self.capture("sku-readback")}

    def read_form(self, product):
        self.product = product
        values, blockers, media_proofs = {}, [], {}
        try:
            values.update(self._identity(product, require_category=False))
            if "category" not in values:
                blockers.append("enter the confirmed product category form before filling")
                return Observation(values, source=self.browser.source, blockers=blockers,
                                   evidence=self.capture("category-entry"))
        except PlatformPause as e:
            blockers.append(str(e))
        for path, binding in self.profile.get("fields", {}).items():
            try:
                values[path] = self._normalize(path, self._read_binding(binding))
            except Exception as e:
                blockers.append({"path": path, "reason": str(e)})
        if self.profile.get('origin'):
            try:
                origin = self.profile['origin']
                actual = self._origin_value()
                expected = origin['province_names'].get(product.value('logistics.province'), '') + ' / ' + origin['city_names'].get(product.value('logistics.city'), '')
                if actual != expected:
                    raise PlatformPause('actual origin differs: ' + actual)
                values['logistics.province'] = product.value('logistics.province')
                values['logistics.city'] = product.value('logistics.city')
            except Exception as e:
                blockers.append({'path': 'logistics.origin', 'reason': str(e)})
        for key, selector in self.profile.get("counts", {}).items():
            try:
                text = self.browser.resolve(selector).inner_text().strip()
                match = re.fullmatch(r"(\d+)\s*/\s*(?:60|30)", text)
                if not match:
                    raise PlatformPause("actual page title counter not readable")
                values[key] = int(match.group(1))
            except Exception as e:
                blockers.append({"path": key, "reason": str(e)})
        for kind in ("main", "detail"):
            try:
                images = self.read_media(product, kind)
                values[kind + "_images"] = images["names"]
                media_proofs[kind] = images
            except Exception as e:
                blockers.append({"path": kind + "_images", "reason": str(e)})
        try:
            actual = self.read_all_skus()
            media_proofs["sku"] = actual
            expected = [{k: product.value(f"skus.{i}.{k}") for k in
                         ("name", "price", "inventory", "area", "cable")}
                        for i in range(len(product.data["skus"]))]
            proof = sku_completeness(expected, actual["rows"], actual["declared_count"])
            values["sku_names"] = [r["name"] for r in actual["rows"]]
            if not proof["ok"]:
                blockers.append({"path": "skus", "reason": proof})
            else:
                by_name = {r["name"]: r for r in actual["rows"]}
                for i, wanted in enumerate(expected):
                    mapped = self._business_units(product, by_name[wanted['name']])
                    for k, value in mapped.items():
                        values[f"skus.{i}.{k}"] = value
        except Exception as e:
            blockers.append({"path": "skus", "reason": str(e)})
        missing = sorted(set(product.expected()) - values.keys())
        if missing:
            blockers.append({"path": "form", "reason": "unread fields", "fields": missing})
        blockers += self.blockers
        return Observation(values, complete=not blockers, source=self.browser.source,
                           evidence={**self.capture("form-readback"), "materials": media_proofs}, blockers=blockers)

    def fill_known(self, product, paths):
        self.product = product
        self._identity(product)
        if self.profile.get("detail_preview"):
            audit = self.audit_detail_preview(product)
            if self.store:
                self.store.event(product.task_id, "detail_preview_audit", audit)
            if not audit["content_order_ok"] and not self._manual_details_current(product, audit):
                raise PlatformPause("detail content/order difference unresolved; no further form writes or submission")
        if self.profile.get("fields", {}).get("fields.title"):
            current = self._read_binding(self.profile["fields"]["fields.title"])
            if current and current != product.value("fields.title"):
                raise PlatformPause("existing title belongs to different data; do not overwrite")
        if self.profile.get("sku"):
            current = self.read_all_skus()
            names = [r.get("name") for r in current["rows"]]
            wanted = product.expected()["sku_names"]
            if len(names) != len(set(names)) or any(n not in wanted for n in names):
                raise PlatformPause("existing SKU conflict; no automatic deletion or expansion")
        self.blockers = []
        for path in paths:
            binding = self.profile.get("fields", {}).get(path)
            if not binding:
                continue
            try:
                value = product.value(path)
                current = self._read_binding(binding)
                if current not in (None, "", "请选择", "请输入") and current != str(value):
                    raise PlatformPause("existing value conflict; preserve current field")
                if current != str(value):
                    if self.store:
                        self.store.event(product.task_id, "field_intent", {"path": path, "value": value})
                    self._write_binding(binding, value)
                if self.store:
                    self.store.event(product.task_id, "field_readback", {"path": path, "actual": self._read_binding(binding)})
            except Exception as e:
                self.blockers.append({"path": path, "reason": str(e)})
                if self.store:
                    self.store.event(product.task_id, "field_pause", {"path": path, "reason": str(e),
                                     "evidence": self.capture("field-stop")})
                raise PlatformPause("field operation paused: " + str(e)) from e
        for kind in ("main", "detail"):
            config = self.profile.get("selected_media", {}).get(kind)
            if not config or not (self.profile.get("materials") or {}).get(kind):
                continue
            container = self.browser.resolve(config["container"])
            if container.locator(config["items"]).count():
                self.read_media(product, kind)
            else:
                if product.data.get('media', {}).get('input_mode') == 'local_folder':
                    raise PlatformPause('local form image upload binding not live verified; do not fall back to material center')
                self.choose_materials(product, kind, product.data["media"][kind])
                self.read_media(product, kind)
        if self.profile.get("spec_creation"):
            self.ensure_skus(product, paths)
        if self.profile.get("batch") and any(p.startswith("skus.") for p in paths):
            try:
                self.fill_batch(product, paths)
            except Exception as e:
                self.blockers.append({"path": "batch", "reason": str(e)})
                raise PlatformPause("batch operation paused: " + str(e)) from e

    def open_sku_batch(self, product, area=False):
        self._identity(product)
        config = self.profile.get('batch') or {}
        if self.store.intent(product.task_id) or not config.get('single_sku_only') or len(product.data['skus']) != 1:
            raise PlatformPause('batch inspection requires one configured SKU and no submission intent')
        before = self.read_all_skus()
        if before['declared_count'] != 1 or [r['name'] for r in before['rows']] != [product.value('skus.0.name')]:
            raise PlatformPause('batch range differs from fresh single-SKU readback')
        ensure_version(product)
        self.store.event(product.task_id, 'batch_open_intent', {'version': product.version, 'before': before})
        if not config.get('region') or self.browser.resolve(config['region'], missing_ok=True) is None:
            self.browser.act(config['open'], 'click')
        if area:
            if 'skus.0.area' not in product.known_paths() or not config.get('area_open'):
                raise PlatformPause('confirmed area and editor binding required')
            self.browser.act(config['area_open'], 'click')
        result = {'state': 'sku_area_opened' if area else 'sku_batch_opened', 'submitted': False, 'source': self.browser.source,
                  'evidence': self.capture('sku-more-batch')}
        self.store.event(product.task_id, 'batch_open', result)
        return result

    def fill_batch(self, product, paths):
        self._identity(product)
        if self.store.intent(product.task_id):
            raise PlatformPause('submission intent exists; batch mutation forbidden')
        config = self.profile.get("batch")
        if not config:
            raise PlatformPause("batch bindings not observed")
        before = self.read_all_skus()
        names = product.expected()["sku_names"]
        actual_names = [r["name"] for r in before["rows"]]
        if before["declared_count"] != len(names) or sorted(names) != sorted(actual_names):
            raise PlatformPause("batch SKU range differs; never apply to old or additional rows")
        if config.get("single_sku_only") is not True or len(names) != 1:
            raise PlatformPause("batch currently restricted to a verified single-SKU range")
        values = {k: product.value("skus.0." + k) for k in config["fields"] if "skus.0." + k in paths}
        if not values:
            return
        ensure_version(product)
        for key, value in values.items():
            current = before['rows'][0].get(key)
            allowed_empty = config.get('empty_values', {}).get(key, [])
            if current not in (None, '') and current not in allowed_empty and current != self._normalize(key, str(value)):
                raise PlatformPause('conflicting existing SKU value: ' + key)
        self.store.event(product.task_id, "batch_intent", {"names": names, "values": values, "before": before})
        self.open_sku_batch(product)
        # Check the entire staged dialog before writing any field, including resumed dialogs.
        for key, value in values.items():
            binding = config['fields'][key]
            current = self._read_binding(binding)
            empties = [None, '', *binding.get('empty_values', [])]
            if current not in empties and self._normalize(key, current) != self._normalize(key, str(value)):
                raise PlatformPause('staged batch conflict: ' + key)
        fresh = self.read_all_skus()
        if fresh['rows'] != before['rows'] or fresh['declared_count'] != before['declared_count']:
            raise PlatformPause('SKU baseline changed while opening batch; preserve current values')
        ordered = sorted(values, key=lambda key: config['fields'][key].get('action') != 'measurement')
        for key in ordered:
            self._write_binding(config["fields"][key], values[key])
        self.browser.act(config["apply"], "click")
        def done():
            rows = self.read_all_skus()["rows"]
            return len(rows) == 1 and all(rows[0].get(k) == self._normalize(k, str(v)) for k, v in values.items())
        self.browser.wait(done)
        after = self.read_all_skus()
        self.store.event(product.task_id, "batch_readback", {"after": after})

    def _image_bytes(self, src):
        if src.startswith("data:image/") and ";base64," in src:
            return base64.b64decode(src.split(",", 1)[1], validate=True)
        parts = urlsplit(src)
        host = parts.hostname or ""
        allowed = ["alicdn.com", "taobaocdn.com"] + self.browser.config.get("allowed_hosts", [])
        if parts.scheme not in ("https", "http") or not any(host == h or host.endswith("." + h) for h in allowed):
            raise PlatformPause("material image URL not on an allowed observed host")
        response = self.browser.context.request.get(src, timeout=15000, max_redirects=0)
        if not response.ok:
            raise PlatformPause("original material unavailable; no thumbnail-only acceptance")
        return response.body()

    def verify_material(self, item, local_path, folder, config):
        filename = item.locator(config["filename"])
        picture = item.locator(config["image"])
        if filename.count() != 1 or picture.count() != 1:
            raise PlatformPause("material filename/preview not unique")
        name = (filename.get_attribute(config["filename_attribute"]) if config.get("filename_attribute")
                else filename.inner_text()).strip()
        src = (picture.get_attribute(config["original_attribute"]) if config.get("original_attribute") else
               picture.evaluate("e => e.currentSrc || e.src"))
        payload = self._image_bytes(src)
        with Image.open(BytesIO(payload)) as im:
            im.load()
            width, height, fmt = im.width, im.height, im.format
        platform_id = item.get_attribute(config["id_attribute"]) if config.get("id_attribute") else src
        local_path = Path(local_path)
        content_ok = same_pixels(payload, local_path)
        return {"ok": bool(platform_id) and name == local_path.name and content_ok,
                "name": name, "folder": folder,
                "platform_id": safe_url(platform_id) if str(platform_id).startswith(('http:', 'https:')) else platform_id,
                "src": safe_url(src) if not src.startswith('data:') else '[embedded image]',
                "width": width, "height": height, "format": fmt, "content_ok": content_ok,
                "remote_sha256": hashlib.sha256(payload).hexdigest(),
                "local_sha256": hashlib.sha256(local_path.read_bytes()).hexdigest(), "time": time.time()}

    def audit_detail_preview(self, product):
        self._identity(product)
        ensure_version(product)
        config = self.profile.get("detail_preview")
        if not config:
            raise PlatformPause("detail preview frame binding not observed")
        frames = [f for f in self.browser.page.frames if re.search(config["frame_url"], f.url)]
        if len(frames) != 1:
            raise PlatformPause("detail preview frame not unique")
        sources = [im.get_attribute("src") for im in frames[0].locator(config["images"]).all()]
        if not sources or any(not s for s in sources):
            raise PlatformPause("detail preview source coverage incomplete")
        groups = []
        for src in sources:
            key = re.sub(r"~crop,\d+,\d+,\d+,\d+~", "", src)
            if not groups or groups[-1]["key"] != key:
                groups.append({"key": key, "sources": []})
            groups[-1]["sources"].append(src)
        root = (product.base / product.data["media"]["root"]).resolve()
        wanted = product.data["media"]["detail"]
        proofs, actual_names = [], []
        for group in groups:
            segments, records = [], []
            for src in group["sources"]:
                self.browser.guard()
                payload = self._image_bytes(src)
                with Image.open(BytesIO(payload)) as im:
                    im.load()
                    segments.append(im.copy())
                    records.append({"src": safe_url(src) if not src.startswith("data:") else "[embedded image]",
                                    "width": im.width, "height": im.height, "format": im.format,
                                    "sha256": hashlib.sha256(payload).hexdigest()})
            matches, candidates = [], []
            joined = Image.new("RGB", (segments[0].width, sum(s.height for s in segments)))
            y = 0
            for segment in segments:
                if segment.width != joined.width:
                    raise PlatformPause("detail segments have inconsistent widths")
                joined.paste(segment.convert("RGB"), (0, y))
                y += segment.height
            for name in wanted:
                local = (root / name).resolve()
                if not local.is_relative_to(root):
                    raise PlatformPause("detail file escapes root")
                with Image.open(local) as original:
                    if verify_segments(original, segments):
                        matches.append(name)
                    if original.size == joined.size:
                        stats = ImageStat.Stat(ImageChops.difference(original.convert("RGB"), joined))
                        candidates.append({"name": name, "mae": sum(stats.mean) / 3,
                                           "rms": sum(stats.rms) / 3, "acceptance": False})
            actual_names.append(matches[0] if len(matches) == 1 else None)
            proofs.append({"segments": records, "matches": matches,
                           "pixel_match_unique": len(matches) == 1,
                           "similarity_candidates": sorted(candidates, key=lambda c: c["rms"])})
        ensure_version(product)
        return {"ok": False, "platform_filenames_verified": False,
                "filename_reason": "preview has no complete platform filename; material metadata still required",
                "content_order_ok": actual_names == wanted and all(p["pixel_match_unique"] for p in proofs),
                "expected_names": wanted, "actual_names": actual_names, "module_count": len(sources),
                "groups": proofs, "source": self.browser.source, "version": product.version,
                "evidence": self.capture("detail-preview-audit")}

    def map_detail_materials(self, product, audit, metadata):
        if audit.get('version') != product.version or metadata.get('folder') != product.data['media']['folders']['detail']:
            raise PlatformPause('detail mapping version/folder differs')
        def asset(src):
            parts = urlsplit(src)
            path = re.sub(r'~crop,\d+,\d+,\d+,\d+~', '', parts.path)
            path = re.sub(r'(\.(?:png|jpe?g))(?:_[^/]*)?$', r'\1', path)
            return (parts.hostname, path)
        wanted = product.data['media']['detail']
        names = {Path(n).name: n for n in wanted}
        if len(names) != len(wanted):
            raise PlatformPause('duplicate local detail filename')
        root = (product.base / product.data['media']['root']).resolve()
        mapped = []
        for group in audit['groups']:
            segments = group['segments']
            keys = {asset(s['src']) for s in segments}
            matches = [m for m in metadata['materials'] if asset(m['preview']) in keys]
            if len(keys) != 1 or len(matches) != 1 or not matches[0].get('material_id'):
                raise PlatformPause('detail asset metadata ambiguous or missing')
            material = matches[0]
            name = names.get(material['filename'])
            if not name:
                raise PlatformPause('detail filename/version conflict: ' + material['filename'])
            local = (root / name).resolve()
            if not local.is_relative_to(root):
                raise PlatformPause('detail image escapes root')
            with Image.open(local) as im:
                size = im.size
            dimensions = re.fullmatch(r'(\d+)x(\d+)', material['dimensions'])
            if not dimensions or tuple(map(int, dimensions.groups())) != size:
                raise PlatformPause('material dimensions differ from configured original')
            offset = 0
            for s in segments:
                crop = re.search(r'~crop,(\d+),(\d+),(\d+),(\d+)~', s['src'])
                if len(segments) > 1:
                    if not crop or tuple(map(int, crop.groups())) != (0, offset, s['width'], s['height']):
                        raise PlatformPause('detail segment geometry incomplete or reversed')
                if s['width'] != size[0]:
                    raise PlatformPause('detail segment width differs')
                offset += s['height']
            if offset != size[1]:
                raise PlatformPause('detail total height differs')
            mapped.append({'name': name, 'material_id': material['material_id'], 'segments': segments,
                           'local_sha256': hashlib.sha256(local.read_bytes()).hexdigest(), 'content_accepted': False})
        actual = [g['name'] for g in mapped]
        if len(actual) != len(set(actual)) or set(actual) != set(wanted):
            raise PlatformPause('detail mapping set incomplete or duplicated')
        return {'ok': False, 'platform_filenames_verified': True, 'actual_names': actual, 'groups': mapped,
                'content_accepted': False, 'sorted': False, 'version': product.version, 'source': audit.get('source'),
                'reason': 'complete filename/asset/geometry verified, reencoded contents still require acceptance'}

    def read_media(self, product, kind):
        ensure_version(product)
        config = self.profile.get("selected_media", {}).get(kind)
        if not config:
            raise PlatformPause("selected image/segment bindings not observed: " + kind)
        container = self.browser.resolve(config["container"])
        items = container.locator(config["items"]).all()
        wanted = product.data["media"][kind]
        by_filename = {}
        for name in wanted:
            filename = Path(name).name
            if filename in by_filename:
                raise PlatformPause("local complete filenames collide in this folder")
            by_filename[filename] = name
        observed, groups = [], []
        for item in items:
            label = item.locator(config["filename"])
            if label.count() != 1:
                raise PlatformPause("selected image filename not uniquely readable")
            filename = (label.get_attribute(config["filename_attribute"]) if config.get("filename_attribute")
                        else label.inner_text()).strip()
            if filename not in by_filename:
                raise PlatformPause("selected extra image or filename/version conflict: " + filename)
            name = by_filename[filename]
            if not observed or observed[-1] != name:
                observed.append(name)
                groups.append([])
            groups[-1].append(item)
        if observed != wanted:
            raise PlatformPause("actual selected image set/order differs or is incomplete")
        root = (product.base / product.data["media"]["root"]).resolve()
        proofs = []
        for name, group in zip(observed, groups):
            local = (root / name).resolve()
            if not local.is_relative_to(root):
                raise PlatformPause("image path escapes product root")
            if len(group) == 1:
                proof = self.verify_material(group[0], local, product.data["media"]["folders"][kind], config)
                if not proof["ok"]:
                    raise PlatformPause("current selected image content not verified: " + name)
                proofs.append(proof)
            else:
                if kind != "detail" or not product.data["media"].get("split_authorized"):
                    raise PlatformPause("unexpected duplicate image or unauthorized segments")
                segments, sources = [], []
                for item in group:
                    pic = item.locator(config["image"])
                    if pic.count() != 1:
                        raise PlatformPause("detail segment preview not unique")
                    src = (pic.get_attribute(config["original_attribute"]) if config.get("original_attribute") else
                           pic.evaluate("e => e.currentSrc || e.src"))
                    payload = self._image_bytes(src)
                    with Image.open(BytesIO(payload)) as im:
                        segments.append(im.convert("RGB"))
                    sources.append({"src": src, "sha256": hashlib.sha256(payload).hexdigest()})
                with Image.open(local) as original:
                    if not verify_segments(original, segments):
                        raise PlatformPause("detail segments omit, reorder or transform original content")
                proofs.append({"name": name, "ok": True, "segments": sources,
                               "local_sha256": hashlib.sha256(local.read_bytes()).hexdigest()})
        return {"names": observed, "proofs": proofs, "evidence": self.capture("selected-" + kind)}

    def choose_materials(self, product, kind, names):
        ensure_version(product)
        config = (self.profile.get("materials") or {}).get(kind)
        if not config:
            raise PlatformPause("material picker bindings not yet observed: " + kind)
        if not config.get("fully_rendered"):
            raise PlatformPause("material candidate coverage not established; pagination/virtual list needs adaptation")
        root = (product.base / product.data["media"]["root"]).resolve()
        folder = product.data["media"]["folders"][kind]
        self.browser.act(config["open"], "click")
        for segment in folder.split("/"):
            selector = dict(config["folder_item"])
            selector["name" if "role" in selector else "text"] = segment
            self.browser.act(selector, "click")
        self.browser.wait(lambda: self.browser.resolve(config["breadcrumb"]).inner_text().strip() == folder)
        container = self.browser.resolve(config["container"])
        proofs = []
        for name in names:
            local = (root / name).resolve()
            if not local.is_relative_to(root):
                raise PlatformPause("image path escapes product root")
            candidates = [i for i in container.locator(config["items"]).all()
                          if i.locator(config["filename"]).count() == 1 and
                          i.locator(config["filename"]).inner_text().strip() == local.name]
            if len(candidates) != 1:
                raise PlatformPause("material full filename missing or duplicated: " + local.name)
            proof = self.verify_material(candidates[0], local, folder, config)
            if not proof["ok"]:
                raise PlatformPause("material content/ID differs: " + local.name)
            proofs.append(proof)
            self.browser.guard()
            self.browser.pace()
            self.browser.guard()
            checkbox = candidates[0].get_by_role("checkbox")
            if checkbox.count() != 1:
                raise LocatorError("material selection checkbox is not unique")
            checkbox.check()
            if not checkbox.is_checked():
                raise PlatformPause("material checkbox did not remain selected")
        self.store.event(product.task_id, "material_selection", {"kind": kind, "proofs": proofs,
                          "evidence": self.capture("material-selection")})
        self.browser.act(config["confirm"], "click")
        if config.get("split_confirm"):
            visible = [e for e in self.browser.page.get_by_text("确认分割", exact=True).all() if e.is_visible()]
            if visible:
                if not product.data["media"].get("split_authorized"):
                    raise PlatformPause("detail split not authorized for this product")
                self.browser.act(config["split_confirm"], "click")
        return proofs

    def ensure_skus(self, product, paths):
        config = self.profile["spec_creation"]
        before = self.read_all_skus()
        existing = [r["name"] for r in before["rows"]]
        desired = product.expected()["sku_names"]
        if len(existing) != len(set(existing)) or any(n not in desired for n in existing):
            raise PlatformPause("existing specifications conflict; do not delete or recreate")
        for index, name in enumerate(desired):
            if name in existing:
                continue
            if not all(f"skus.{index}.{k}" in paths for k in ("name", "image")):
                self.blockers.append({"path": f"skus.{index}", "reason": "name/image evidence missing for specification creation"})
                continue
            self.store.event(product.task_id, "spec_creation_intent", {"name": name, "before": before})
            self.browser.act(config["open"], "click")
            self._write_binding(config["name"], name)
            self.choose_materials(product, "sku", [product.value(f"skus.{index}.image")])
            self.browser.act(config["confirm"], "click")
            self.browser.wait(lambda: name in [r["name"] for r in self.read_all_skus()["rows"]])
            existing.append(name)
            self.store.event(product.task_id, "spec_created_readback", self.read_all_skus())

    def submit_once(self, token):
        if not token or not self.store:
            raise PlatformPause("persisted submission token required")
        if not self.profile.get("submit", {}).get("verified") or not self.product:
            raise PlatformPause("submit bindings and full live feasibility must be verified first")
        intent = self.store.intent(self.product.task_id)
        if not intent or intent["token"] != token or intent["version"] != self.product.version:
            raise PlatformPause("submission token/version differs")
        from .workflow import compare
        fresh = self.read_form(self.product)
        if not compare(self.product, fresh)["ok"]:
            raise PlatformPause("actual pre-click full readback failed")
        self.store.observe(self.product, compare(self.product, fresh)["checks"], fresh.source, fresh.evidence, fresh.observed)
        self.store.event(self.product.task_id, "pre_click_observation", {"token": token, "evidence": fresh.evidence,
                         "checks": compare(self.product, fresh)["checks"]})
        ensure_version(self.product)
        self.store.claim_click(token)
        self.browser.act(self.profile["submit"]["selector"], "click", before_action=lambda: ensure_version(self.product))
        self.browser.wait(lambda: bool(self._receipt_id()), timeout=self.profile["submit"].get("timeout", 60))
        return {"item_id": self._receipt_id(), "evidence": self.capture("submit-receipt"), "source": self.browser.source}

    def prepare_submit(self, product):
        if not self.profile.get("submit", {}).get("verified") or not (self.profile.get("warehouse") or {}).get("verified"):
            raise PlatformPause("live submission and warehouse bindings must be accepted before reserving an intent")
        if not self.profile.get("edit", {}).get("status"):
            raise PlatformPause("edit-page status/identity bindings required before submission")
        self._identity(product)
        self.browser.resolve(self.profile["submit"]["selector"])
        self.store.event(product.task_id, "submit_authorization", {"source": "explicit submit command",
                         "task_id": product.task_id, "version": product.version,
                         "scope": "fill, fully verify, submit once to warehouse, reconcile; never list immediately"})

    def _receipt_id(self):
        config = self.profile.get("submit", {})
        pattern = config.get("receipt_url_pattern")
        if not pattern or not re.search(pattern, self.browser.page.url):
            return None
        query = parse_qs(urlsplit(self.browser.page.url).query)
        ids = query.get(config.get("item_id_parameter", "itemId"), [])
        excluded = {"632745637833", "50021001"}
        if self.product and self.store:
            intent = self.store.intent(self.product.task_id)
            if intent:
                excluded |= set(intent["baseline"]["ids"])
        return ids[0] if len(ids) == 1 and re.fullmatch(r"\d{10,20}", ids[0]) and ids[0] not in excluded else None

    def warehouse_baseline(self, product):
        config = self.profile.get("warehouse")
        if not config or not config.get("verified"):
            raise PlatformPause("warehouse pagination/status bindings not yet verified")
        original = self.browser.page
        try:
            pages = [p for p in self.browser.context.pages if re.search(config["page_url_pattern"], p.url)]
            if len(pages) != 1:
                raise PlatformPause("warehouse tab not unique")
            self.browser.page = pages[0]
            if not self._warehouse_selected():
                self.browser.act(config["tab"], "click")
                self.browser.wait(self._warehouse_selected)
            first = self._scan_warehouse(product)
            second = self._scan_warehouse(product)
            if first["ids"] != second["ids"] or first["total"] != second["total"]:
                raise PlatformPause("warehouse changed during pagination; completeness not proved")
            self.warehouse_records = second["rows"]
            if self.store:
                self.store.event(product.task_id, "warehouse_query", second)
            return second
        finally:
            self.browser.page = original

    def audit_warehouse(self, product):
        """Exercise candidate pagination bindings without enabling submission."""
        self.open_product_list(product, warehouse=True)
        config = self.profile.get('warehouse')
        if not config:
            raise PlatformPause('warehouse candidate bindings missing')
        original = self.browser.page
        try:
            pages = [p for p in self.browser.context.pages if re.search(config['page_url_pattern'], p.url)]
            if len(pages) != 1:
                raise PlatformPause('warehouse tab not unique')
            self.browser.page = pages[0]
            self.browser.wait(lambda: not self._loading())
            first = self._scan_warehouse(product)
            second = self._scan_warehouse(product)
            if first['ids'] != second['ids'] or first['total'] != second['total']:
                raise PlatformPause('warehouse changed during pagination')
            result = {**second, 'state': 'warehouse_pagination_observed', 'submitted': False,
                      'submit_adapter_verified': False}
            self.store.event(product.task_id, 'warehouse_pagination_audit', result)
            return result
        finally:
            self.browser.page = original

    def open_product_list(self, product, warehouse=False):
        self._identity(product, require_category=False)
        config = self.profile.get('warehouse_entry') or {}
        if not config.get('url') or not config.get('page_url_pattern'):
            raise PlatformPause('observed product-list entry required')
        self.browser._host(config['url'])
        original = self.browser.page
        targets = [p for p in self.browser.context.pages if re.search(config['page_url_pattern'], p.url)]
        if len(targets) > 1:
            raise PlatformPause('product-list tab not unique')
        self.store.event(product.task_id, 'product_list_open_intent', {'url': safe_url(config['url']),
                         'scope': 'read-only warehouse adaptation; preserve unsaved form'})
        page = targets[0] if targets else self.browser.context.new_page()
        try:
            self.browser.page = page
            if not targets:
                self.browser.goto(config['url'])
            def shop_ready():
                matches = self._shop_matches(product)
                if len(matches) > 1:
                    raise PlatformPause('product-list shop identity ambiguous')
                return len(matches) == 1
            self.browser.wait(shop_ready, timeout=20)
            self._identity(product, require_category=False)
            if warehouse:
                if not config.get('tab'):
                    raise PlatformPause('warehouse tab control not observed')
                tab = self.browser.resolve(config['tab'])
                if not tab.inner_text().strip().startswith('仓库中'):
                    raise PlatformPause('warehouse tab label differs')
                if tab.get_attribute('aria-selected') != 'true':
                    self.browser.act(config.get('tab_click', config['tab']), 'click')
                self.browser.wait(lambda: self.browser.resolve(config['tab']).get_attribute('aria-selected') == 'true')
            result = {'state': 'warehouse_tab_selected' if warehouse else 'product_list_opened', 'shop': product.value('shop'), 'url': safe_url(page.url),
                      'warehouse_verified': False, 'submitted': False, 'source': self.browser.source,
                      'evidence': self.capture('product-list-current')}
            self.store.event(product.task_id, 'product_list_opened', result)
            return result
        finally:
            self.browser.page = original

    def _warehouse_selected(self):
        c = self.profile["warehouse"]
        control = self.browser.resolve(c["selected_tab"])
        attribute = c.get("active_attribute", "aria-selected")
        value = control.get_attribute(attribute) or ""
        active = (value == "true" if attribute == "aria-selected" else
                  bool(c.get("active_pattern") and re.search(c["active_pattern"], value)))
        return active and control.inner_text().strip().startswith("仓库中")

    def _scan_warehouse(self, product):
        c = self.profile["warehouse"]
        self.browser.guard()
        shop = self._shop_matches(product)
        if len(shop) != 1:
            raise PlatformPause("warehouse shop identity not unique")
        if c.get("search_title"):
            self.browser.act(c["search_title"], "fill", product.value("fields.title"))
            self.browser.act(c["search"], "click")
            self.browser.wait(lambda: not self._loading())
        if self.browser.resolve(c['page_number']).inner_text().strip() != '1':
            self._turn_warehouse_page(c['first_page'], '1')
        total_text = self.browser.resolve(c["total"]).inner_text().strip()
        total_match = re.fullmatch(c["total_pattern"], total_text)
        if not total_match:
            raise PlatformPause("warehouse total not readable")
        total = int(total_match.group(1))
        records, evidence, pages_seen = [], [], set()
        for _ in range(c.get("max_pages", 100)):
            self.browser.guard()
            if not self._warehouse_selected():
                raise PlatformPause("warehouse tab no longer selected")
            page_number = self.browser.resolve(c["page_number"]).inner_text().strip()
            if page_number in pages_seen:
                raise PlatformPause("warehouse pagination repeated page")
            pages_seen.add(page_number)
            rows = self.browser.resolve(c["table"]).locator(c["rows"])
            entries = rows.evaluate_all('''(es,c)=>es.map(row=>({text:row.innerText,
                created:[...row.querySelectorAll(c.created)].map(e=>e.innerText.trim()),
                titles:[...row.querySelectorAll(c.title)].map(e=>e.innerText.trim()),
                statuses:c.status?[...row.querySelectorAll(c.status)].map(e=>e.innerText.trim()):null,
                edits:[...row.querySelectorAll('a')].filter(e=>e.innerText.trim()==='编辑商品').map(e=>e.getAttribute('href'))}))''',
                {'created': c['created'], 'title': c['title'], 'status': c.get('row_status')})
            for entry in entries:
                text = entry['text']
                if c.get('row_status') and entry['statuses'] != ['仓库中']:
                    raise PlatformPause('warehouse row status differs; old or changing table not accepted')
                ids = re.findall(c.get("id_pattern", r"ID:\s*(\d{10,20})"), text)
                if len(ids) != 1:
                    raise PlatformPause("warehouse row ID missing or ambiguous")
                if len(entry['created']) != 1 or len(entry['titles']) != 1:
                    raise PlatformPause('warehouse row fields not uniquely observable')
                created_text = entry['created'][0]
                from datetime import datetime, timezone, timedelta
                created = datetime.strptime(created_text, c.get("created_format", "%Y-%m-%d %H:%M"))
                created = created.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
                title = entry['titles'][0]
                if len(entry['edits']) != 1 or not entry['edits'][0]:
                    raise PlatformPause("warehouse editor link not uniquely observable")
                from urllib.parse import urljoin
                edit_url = urljoin(self.browser.page.url, entry['edits'][0])
                self.browser._host(edit_url)
                records.append({"item_id": ids[0], "shop": product.value("shop"), "warehouse": self._warehouse_selected(),
                                "title": title, "created_at": created, "edit_url": edit_url,
                                "source": self.browser.source, "page": page_number})
            evidence.append(self.capture("warehouse-page"))
            if self.store:
                self.store.event(product.task_id, 'warehouse_page_observed', {'page': page_number,
                    'ids': [r['item_id'] for r in records if r['page'] == page_number], 'source': self.browser.source})
            if self.browser.resolve(c["total"]).inner_text().strip() != total_text:
                raise PlatformPause("warehouse total changed while reading")
            next_page = self.browser.resolve(c["next_page"])
            if not next_page.is_enabled() or next_page.get_attribute("aria-disabled") == "true":
                break
            self._turn_warehouse_page(c['next_page'], str(int(page_number) + 1))
        else:
            raise PlatformPause("warehouse exceeded pagination limit")
        ids = [r["item_id"] for r in records]
        if len(ids) != total or len(set(ids)) != total:
            raise PlatformPause(f"warehouse unique IDs/read count/declared total differ: read={len(ids)}, unique={len(set(ids))}, total={total}, pages={sorted(pages_seen)}")
        for r in records:
            r["query_complete"] = True
        return {"ids": sorted(ids), "total": total, "complete": True, "rows": records,
                "evidence": evidence, "source": self.browser.source, "time": time.time()}

    def _turn_warehouse_page(self, selector, expected_page):
        c = self.profile['warehouse']
        def fingerprint():
            texts = self.browser.resolve(c['table']).locator(c['rows']).evaluate_all('(es)=>es.map(e=>e.innerText)')
            pattern = c.get('id_pattern', r'ID:\s*(\d{10,20})')
            ids = [re.findall(pattern, text) for text in texts]
            return tuple(row[0] for row in ids) if ids and all(len(row) == 1 for row in ids) else ()
        before = fingerprint()
        self.browser.act(selector, 'click')
        previous = None
        def settled():
            nonlocal previous
            if self._loading() or self.browser.resolve(c['page_number']).inner_text().strip() != expected_page:
                previous = None
                return False
            actual = fingerprint()
            if not actual or actual == before:
                previous = None
                return False
            stable = actual == previous
            previous = actual
            return stable
        self.browser.wait(settled)

    def _loading(self):
        selector = self.profile.get("warehouse", {}).get("loading")
        if not selector:
            return False
        return bool([e for e in self.browser._make(self.browser.page, selector).all() if e.is_visible()])

    def find_warehouse(self, product, intent):
        c = self.profile.get("warehouse") or {}
        deadline = time.monotonic() + c.get("settle_timeout", 15)
        while True:
            result = self.warehouse_baseline(product)
            candidates = [r for r in result["rows"] if r["item_id"] not in intent["baseline"]["ids"]
                          and r["title"] == product.value("fields.title")]
            if candidates or time.monotonic() >= deadline:
                return candidates
            original = self.browser.page
            try:
                targets = [p for p in self.browser.context.pages if re.search(c["page_url_pattern"], p.url)]
                if len(targets) != 1:
                    raise PlatformPause("warehouse tab changed during reconciliation")
                self.browser.page = targets[0]
                self.browser.pace()
                self.browser.page.reload(wait_until="domcontentloaded")
            finally:
                self.browser.page = original
            time.sleep(1)

    def read_edit(self, product, item_id):
        rows = [r for r in getattr(self, "warehouse_records", []) if r["item_id"] == item_id]
        if len(rows) != 1:
            raise PlatformPause("no unique observed warehouse editor link")
        config = self.profile.get("edit") or {}
        if not config.get("status"):
            raise PlatformPause("edit-page warehouse status binding not observed")
        original = self.browser.page
        page = self.browser.context.new_page()
        try:
            self.browser.page = page
            self.browser.goto(rows[0]["edit_url"])
            self.browser.guard()
            ids = parse_qs(urlsplit(page.url).query).get(config.get("item_id_parameter", "itemId"), [])
            if ids != [item_id]:
                raise PlatformPause("edit-page item ID differs")
            result = self.read_form(product)
            result.item_id = ids[0]
            result.warehouse = self._value(self.browser.resolve(config["status"])) == "仓库中"
            return result
        finally:
            self.browser.page = original
            page.close()
