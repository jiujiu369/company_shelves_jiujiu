"""Loopback-only workbench for the existing CLI and durable evidence."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .data import Product, load_product, validate
from .media import manifest
from .runtime import ProcessLock, configuration_digest
from .store import Store


ASSETS = Path(__file__).parent / 'web'
MANUAL = {
    'shop': ('登录正确店铺', '在专用浏览器手动登录，确认店铺名称；验证码或登录失效时交给人工处理。'),
    'materials': ('整理素材与版本', '核对主图、详情、SKU文件夹及完整文件名；重名、额外图片和旧版本不能混用。长图分割须有授权。'),
    'units': ('确认价格、库存与单位', '正式值须有来源；销售单位、库存单位和每包只数分别确认。JG的100元、999库存不可沿用为正式默认值。'),
    'exclusive': ('保留现场，停止同时编辑', '保留当前商品表单；任务运行时不要同时手工修改。电脑保持开机、不休眠，浏览器保持打开。'),
}
FIELD_NAMES = {
    'shop': '店铺', 'category': '类目', 'title': '宝贝标题', 'guide_title': '导购标题',
    'brand': '真实品牌', 'model': '型号', 'installation': '安装方式', 'conductor': '导体材质',
    'contact_plating': '触点镀层', 'price': '价格', 'inventory': '库存', 'sales_unit': '销售单位',
    'inventory_unit': '库存单位', 'area': '导线截面积', 'cable': '适用线缆', 'pack_quantity': '每包数量',
    'image': 'SKU图片', 'name': '规格名称', 'province': '省份', 'city': '城市', 'template': '运费模板',
    'dispatch': '发货时效', 'listing': '入仓方式', 'preflight': '提交前完整核对',
    'completeness': '读取完整性', 'main_images': '主图映射', 'detail_images': '详情映射',
    'total_inventory': '总库存', 'title_count': '标题计数', 'guide_title_count': '导购计数', 'sku_names': '完整规格组合',
}


def field_name(path):
    parts = path.split('.')
    prefix = f'SKU {int(parts[1]) + 1} · ' if len(parts) > 2 and parts[0] == 'skus' and parts[1].isdigit() else ''
    return prefix + FIELD_NAMES.get(parts[-1], path)


def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return fallback


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class Dashboard:
    manual_preparations = tuple(MANUAL)
    actions = ('validate', 'start-browser', 'probe', 'inspect', 'audit-images', 'fill', 'resume', 'submit', 'open-warehouse-tab', 'audit-warehouse', 'model-probe', 'model-extract', 'model-suggest')

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config_path = self.root / 'config.json'
        self.config = read_json(self.config_path)
        if not isinstance(self.config, dict):
            raise ValueError('valid project configuration required')
        self.config_digest = configuration_digest(self.config_path)
        self.state_path = (self.root / self.config['state']).resolve()
        self.jobs_dir = self.root / '运行记录' / 'ui-jobs'
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.RLock()
        self.job = None
        self.selection_active = False

    def configuration(self, name):
        p = self.product(name)
        return {'product': name, 'version': p.version, 'data': p.data}

    def _editable(self):
        if self.job and self.job['status'] == 'running' or self.selection_active:
            raise ValueError('请等待当前任务或资料选择完成')
        if configuration_digest(self.config_path) != self.config_digest:
            raise ValueError('runtime configuration changed; restart the workbench')

    @staticmethod
    def _shape(data):
        if not isinstance(data, dict) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', str(data.get('task_id', ''))):
            raise ValueError('safe task identifier required')
        for section in ('fields', 'logistics', 'media'):
            if not isinstance(data.get(section, {}), dict):
                raise ValueError(section + ' must be an object')
        if not isinstance(data.get('skus', []), list) or any(not isinstance(s, dict) for s in data.get('skus', [])):
            raise ValueError('skus must be a list of objects')
        media = data.get('media', {})
        if not isinstance(media.get('root', '.'), str):
            raise ValueError('media.root must be a path')
        for key in ('main', 'detail'):
            if not isinstance(media.get(key, []), list) or any(not isinstance(s, str) for s in media.get(key, [])):
                raise ValueError('media.' + key + ' must be a filename list')
        # Validate without adding defaults; missing business facts remain pending.
        try:
            validate(Product(data))
        except (TypeError, AttributeError, KeyError, ValueError) as error:
            raise ValueError('invalid configuration structure: ' + str(error)) from error

    def save_configuration(self, name, version, data):
        with self.mutex, ProcessLock(self.root / '运行记录' / 'run.lock'):
            self._editable()
            old = self.product(name)
            if old.version != version:
                raise ValueError('product version changed; reload configuration')
            self._shape(data)
            if data['task_id'] != old.task_id:
                raise ValueError('task identity cannot change; import as a separate task')
            intent = self._records(old.task_id)['submission']
            if intent and intent['state'] != 'manual_not_submitted':
                raise ValueError('submission already recorded; resolve before editing configuration')
            try:
                manifest(Product(data, old.base))
            except (TypeError, ValueError, OSError) as error:
                raise ValueError('invalid material configuration: ' + str(error)) from error
            archive = self.root / '运行记录' / 'ui-config-revisions' / old.task_id / (old.version + '.json')
            if not archive.exists():
                write_json(archive, old.data)
            write_json(self.product_path(name), data)
            p = self.product(name)
            issues = [i.to_dict() for i in validate(p)] + manifest(p)['issues']
            state = 'pending_data' if any(i.get('blocking', True) for i in issues) else 'data_ready'
            s = Store(self.state_path)
            try:
                s.register(p)
                s.state(p.task_id, state)
                s.event(p.task_id, 'ui_configuration_saved', {'from': old.version, 'to': p.version,
                    'archive': str(archive), 'source': 'local UI operator', 'platform_verified': False})
            finally:
                s.close()
            return {'saved': True, 'product': name, 'version': p.version, 'state': state,
                    'archive': str(archive.relative_to(self.root)), 'issues': issues}

    def import_configuration(self, source_path):
        with self.mutex, ProcessLock(self.root / '运行记录' / 'run.lock'):
            self._editable()
            source = Path(source_path).resolve(strict=True)
            if source.suffix.lower() != '.json' or source.stat().st_size > 1048576:
                raise ValueError('请选择不超过1MB的商品JSON配置')
            data = json.loads(source.read_text(encoding='utf-8-sig'))
            self._shape(data)
            task_id = data['task_id']
            if any(self.product(name).task_id == task_id for name in self.products()) or self._records(task_id)['task']:
                raise ValueError('task already exists; select its existing configuration')
            data.setdefault('media', {})['root'] = str((source.parent / data.get('media', {}).get('root', '.')).resolve())
            try:
                manifest(Product(data, source.parent))
            except (TypeError, ValueError, OSError) as error:
                raise ValueError('invalid material configuration: ' + str(error)) from error
            name = task_id + '.json'
            target = self.root / 'products' / name
            if target.exists():
                raise ValueError('configuration filename already exists')
            write_json(target, data)
            p = self.product(name)
            s = Store(self.state_path)
            try:
                s.register(p)
                s.event(task_id, 'ui_configuration_imported', {'source': str(source), 'version': p.version,
                    'platform_verified': False})
            finally:
                s.close()
            return {'product': name, 'version': p.version}

    def select_local(self, kind):
        if kind not in ('file', 'folder'):
            raise ValueError('file or folder selection required')
        with self.mutex:
            self._editable()
            self.selection_active = True
        try:
            result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'qianniu.picker', kind],
                cwd=Path(__file__).parent.parent, capture_output=True, text=True, encoding='utf-8', timeout=180,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('本机选择窗口无法打开，请手动输入路径')
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired as error:
            raise ValueError('选择窗口已超时，请重新选择或手动输入路径') from error
        finally:
            with self.mutex:
                self.selection_active = False

    def products(self):
        return [p.name for p in sorted((self.root / 'products').glob('*.json')) if p.is_file() and p.resolve().is_relative_to(self.root / 'products')]

    def product_path(self, name):
        if not isinstance(name, str) or name not in self.products():
            raise ValueError('请选择products目录中的商品配置')
        return self.root / 'products' / name

    def product(self, name):
        product = load_product(self.product_path(name))
        if not isinstance(product.task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', product.task_id):
            raise ValueError('safe task identifier required')
        return product

    def _records(self, task_id):
        if not self.state_path.exists():
            return {'task': None, 'checks': [], 'events': [], 'submission': None}
        # Reads neither initialize the schema nor register a newer product revision.
        with closing(sqlite3.connect(self.state_path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            checks = [dict(r) for r in db.execute('SELECT * FROM checks WHERE task_id=? ORDER BY path', (task_id,))]
            events = [dict(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 100', (task_id,))]
            submission = db.execute('SELECT attempt,state,version,created FROM intents WHERE task_id=? ORDER BY attempt DESC LIMIT 1', (task_id,)).fetchone()
        for c in checks:
            for k in ('expected', 'actual', 'evidence'):
                c[k] = json.loads(c[k])
            c['label'] = field_name(c['path'])
        for e in events:
            e['payload'] = json.loads(e['payload'])
        return {'task': dict(task) if task else None, 'checks': checks, 'events': events,
                'submission': dict(submission) if submission else None}

    def snapshot(self, name):
        if configuration_digest(self.config_path) != self.config_digest:
            raise ValueError('runtime configuration changed; restart the workbench before continuing')
        p = self.product(name)
        images = manifest(p)
        issues = [i.to_dict() for i in validate(p)] + images['issues']
        for issue in issues:
            issue['label'] = field_name(issue['path'])
        records = self._records(p.task_id)
        ack = next((e['payload'] for e in records['events'] if e['kind'] == 'ui_preparation_ack'), {})
        checked = ack.get('checks', []) if ack.get('version') == p.version else []
        blocked = [i for i in issues if i.get('blocking', True)]
        preparations = [
            {'id': 'data', 'title': '商品资料与来源', 'detail': f'必填待补 {len(blocked)} 项；可选待补 {len(issues) - len(blocked)} 项。未知值不虚构，已知独立步骤可继续。', 'manual': False, 'ok': not blocked},
            {'id': 'media', 'title': '本地图片检查', 'detail': f'配置图片 {len(images["files"])} 张；额外图片 {len(images["extras"])} 张，只列清单，不自动新增SKU。', 'manual': False, 'ok': not images['issues']},
            {'id': 'logistics', 'title': '物流与放入仓库', 'detail': ' / '.join(str(p.value('logistics.' + key) or '待补') for key in ('province', 'city', 'dispatch', 'template', 'listing')), 'manual': False,
             'ok': p.value('logistics.listing') == '放入仓库' and not any(i['path'].startswith('logistics.') and i.get('blocking', True) for i in issues)},
        ] + [{'id': key, 'title': title, 'detail': detail, 'manual': True, 'ok': key in checked} for key, (title, detail) in MANUAL.items()]
        profile = read_json(self.root / self.config['profile'], {})
        if p.data.get('media', {}).get('input_mode') == 'local_folder':
            preparations.append({'id': 'local-entry', 'title': '以图发品本地上传', 'manual': False,
                'ok': profile.get('entry', {}).get('local_upload', {}).get('verified') is True,
                'detail': '起点：以图发品；只上传配置清单。入口预览、类目识别与表单图片绑定尚待现场验证。'})
        preflight = next((c for c in records['checks'] if c['path'] == 'preflight'), None)
        fresh = bool(preflight and preflight['ok'] and preflight['source'] == 'live_playwright'
                     and preflight['version'] == p.version and 0 <= time.time() - preflight['observed'] <= 60)
        unresolved = bool(records['submission'] and records['submission']['state'] != 'manual_not_submitted')
        confirmed_current = bool(records['task'] and records['task']['state'] == 'confirmed_warehouse'
                                 and records['submission'] and records['submission']['state'] == 'confirmed_warehouse'
                                 and records['submission']['version'] == p.version)
        prep_ready = set(self.manual_preparations).issubset(checked)
        submit_ready = bool(prep_ready and not blocked and fresh and profile.get('submit', {}).get('verified') is True and profile.get('warehouse') and not unresolved)
        facts = [{'path': key, 'label': field_name(key), **fact} for key, fact in p.facts().items() if isinstance(fact, dict)]
        for check in records['checks']:
            check['current_version'] = check['version'] == p.version
        evidence_dir = self.root / '运行记录' / p.task_id / 'observations'
        screenshots = sorted(evidence_dir.glob('*.png'), key=lambda f: f.stat().st_mtime, reverse=True)[:6] if evidence_dir.is_dir() else []
        screenshot = screenshots[0].name if screenshots else None
        advice = next((e['payload'] for e in records['events'] if e['kind'] == 'model_advice' and e['payload'].get('version') == p.version), None)
        return {**records, 'model': {'url': self.config.get('ollama', {}).get('url'), 'name': self.config.get('ollama', {}).get('model'),
                'advice': advice, 'images': [name for name, meta in images['files'].items() if 'error' not in meta]},
                'product': name, 'task_id': p.task_id, 'version': p.version, 'mode': p.data.get('mode'),
                'shop': p.value('shop'), 'title': p.value('fields.title'), 'issues': issues, 'preparations': preparations,
                'preparation_ready': prep_ready, 'submit_ready': submit_ready, 'fresh_preflight': fresh,
                'confirmed_current_version': confirmed_current,
                'submit_adapter_verified': profile.get('submit', {}).get('verified') is True,
                'facts': facts, 'media': images, 'screenshot': screenshot,
                'sku_count': len(p.data.get('skus', [])), 'main_image': next(iter(p.data.get('media', {}).get('main', [])), None),
                'job': self.get_job(), 'time': time.time()}

    def acknowledge(self, name, version, checks):
        with self.mutex:
            if self.job and self.job['status'] == 'running':
                raise ValueError('任务正在执行，请先暂停再修改准备确认')
            p = self.product(name)
            if version != p.version:
                raise ValueError('product version changed; refresh before confirming')
            if not isinstance(checks, list) or any(c not in MANUAL for c in checks):
                raise ValueError('unknown preparation checkbox')
            with ProcessLock(self.root / '运行记录' / 'run.lock'):
                s = Store(self.state_path)
                try:
                    s.register(p)
                    s.event(p.task_id, 'ui_preparation_ack', {'version': p.version, 'checks': sorted(set(checks)),
                        'source': 'local UI operator confirmation; not platform evidence', 'platform_verified': False})
                finally:
                    s.close()
        return {'saved': True}

    def get_job(self):
        with self.mutex:
            if self.job:
                return dict(self.job)
            paths = sorted(self.jobs_dir.glob('*/job.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            previous = read_json(paths[0]) if paths else None
            if previous and previous.get('status') == 'running':
                previous = {**previous, 'status': 'unconfirmed', 'pause_available': True, 'reason': 'UI restarted; inspect durable records and current platform, never restart submission automatically'}
            return previous

    def start(self, name, action, version=None, file=None):
        if action not in self.actions:
            raise ValueError('unsupported command')
        with self.mutex:
            if self.selection_active:
                raise ValueError('请先完成本机资料选择')
            if self.job and self.job['status'] == 'running':
                raise ValueError('已有任务正在执行，不能重复启动')
            view = self.snapshot(name)
            if version is not None and version != view['version']:
                raise ValueError('displayed product version changed; refresh before starting')
            if action in ('fill', 'submit'):
                if view['submission'] and view['submission']['state'] != 'manual_not_submitted':
                    raise ValueError('unresolved submission; only recover/query is permitted')
                if not view['preparation_ready']:
                    raise ValueError('请先完成开始前的四项人工准备确认')
            if action == 'resume' and (not view['submission'] or view['submission']['state'] == 'manual_not_submitted') and not view['preparation_ready']:
                raise ValueError('未提交任务的恢复可能填写页面，请先完成准备确认')
            if action in ('fill', 'resume', 'submit') and version is None:
                raise ValueError('displayed product version required before browser writes')
            if action == 'submit' and not view['submit_ready']:
                raise ValueError('真实全面核对或仓库适配未通过，禁止提交')
            if action == 'model-extract':
                if not isinstance(file, str) or file not in view['model']['images']:
                    raise ValueError('请选择当前商品配置中的有效图片')
                self.media_file(name, file)
            # Check the existing cross-process lock before creating any job.
            with ProcessLock(self.root / '运行记录' / 'run.lock'):
                pass
            job_id = uuid.uuid4().hex
            directory = self.jobs_dir / job_id
            directory.mkdir(parents=True)
            command = [sys.executable, '-X', 'utf8', '-m', 'qianniu', '--config', str(self.config_path),
                       '--expected-config-digest', self.config_digest]
            if action not in ('start-browser', 'probe', 'model-probe'):
                command += ['--expected-product-version', view['version']]
            command += [action]
            if action not in ('start-browser', 'probe', 'model-probe'):
                command += [str(self.product_path(name)), '--output', str(directory / 'result.json')]
            if action == 'model-extract':
                command += ['--file', file]
            job = {'id': job_id, 'action': action, 'product': name, 'task_id': view['task_id'],
                   'status': 'running', 'started': time.time(), 'exit_code': None, 'result': None}
            log = (directory / 'output.log').open('wb')
            try:
                process = subprocess.Popen(command, cwd=Path(__file__).parent.parent, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except BaseException:
                log.close()
                raise
            job['pid'] = process.pid
            self.job = job
            write_json(directory / 'job.json', job)
            threading.Thread(target=self._finish, args=(process, log, directory, job), daemon=True).start()
            return dict(job)

    def _finish(self, process, log, directory, job):
        code = process.wait()
        log.close()
        result = read_json(directory / 'result.json')
        if result is None:
            try:
                result = json.loads((directory / 'output.log').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                result = {'reason': '命令未返回完整结果，请查看执行记录'}
        with self.mutex:
            job.update(status='finished', ended=time.time(), exit_code=code, result=result)
            write_json(directory / 'job.json', job)

    def pause(self, name):
        p = self.product(name)
        with self.mutex:
            job = self.get_job()
            if job and job['status'] in ('running', 'unconfirmed') and job['task_id'] != p.task_id:
                raise ValueError('请切换到正在执行的商品再暂停')
            write_json(self.root / '运行记录' / 'STOP', {'task_id': p.task_id, 'time': time.time()})
        return {'state': 'pause_requested', 'reason': '在下一次页面操作前暂停；已发出的提交仍需回查'}

    def media_file(self, name, filename):
        p = self.product(name)
        media = p.data.get('media', {})
        names = media.get('main', []) + media.get('detail', []) + [p.value(f'skus.{i}.image') for i in range(len(p.data.get('skus', [])))]
        root = (p.base / media.get('root', '.')).resolve()
        path = (root / filename).resolve()
        if filename not in names or not path.is_relative_to(root) or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ValueError('configured image required')
        return path

    def evidence_file(self, name, filename):
        p = self.product(name)
        root = (self.root / '运行记录' / p.task_id / 'observations').resolve()
        path = (root / filename).resolve()
        if Path(filename).name != filename or not path.is_relative_to(root) or path.suffix.lower() != '.png':
            raise ValueError('screenshot evidence required')
        return path


def make_server(app, port=8766):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, data, content_type='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.trusted_host() or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.respond(403, {'error': '仅允许本机同源访问'})
                return
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            try:
                if path.path == '/api/bootstrap':
                    self.respond(200, {'token': app.token, 'products': app.products(), 'job': app.get_job()})
                elif path.path == '/api/state':
                    self.respond(200, app.snapshot(query.get('product', [''])[0]))
                elif path.path == '/api/configuration':
                    self.respond(200, app.configuration(query.get('product', [''])[0]))
                elif path.path in ('/api/image', '/api/evidence'):
                    method = app.media_file if path.path == '/api/image' else app.evidence_file
                    file = method(query.get('product', [''])[0], query.get('file', [''])[0])
                    self.respond(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'image/png')
                elif path.path in ('/', '/app.js', '/style.css', '/lucide.min.js'):
                    file = ASSETS / ('index.html' if path.path == '/' else path.path[1:])
                    self.respond(200, file.read_bytes(), (mimetypes.guess_type(file.name)[0] or 'text/plain') + '; charset=utf-8')
                else:
                    self.respond(404, {'error': '页面不存在'})
            except (ValueError, OSError, sqlite3.Error) as e:
                self.respond(400, {'error': str(e)})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if not self.trusted_host() or self.headers.get('Origin') != origin or self.headers.get('X-UI-Token') != app.token:
                self.respond(403, {'error': '仅允许本机工作台发起操作'})
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 1048576:
                    raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                if self.path == '/api/preparation':
                    result = app.acknowledge(body['product'], body['version'], body['checks'])
                elif self.path == '/api/action':
                    result = app.start(body['product'], body['action'], version=body['version'], **({'file': body['file']} if 'file' in body else {}))
                elif self.path == '/api/pause':
                    result = app.pause(body['product'])
                elif self.path == '/api/select':
                    result = app.select_local(body['kind'])
                elif self.path == '/api/import-path':
                    result = app.import_configuration(body['path'])
                elif self.path == '/api/save-configuration':
                    result = app.save_configuration(body['product'], body['version'], body['data'])
                else:
                    self.respond(404, {'error': '未知操作'})
                    return
                self.respond(200, result)
            except (KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error) as e:
                self.respond(409, {'error': str(e)})
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='千牛自动录入可视化工作台')
    parser.add_argument('--root', default=str(Path(__file__).parent.parent))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    app = Dashboard(args.root)
    for port in range(args.port, args.port + 20):
        try:
            server = make_server(app, port)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('没有可用的本机工作台端口')
    url = f'http://127.0.0.1:{server.server_port}'
    print('千牛工作台：' + url, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
