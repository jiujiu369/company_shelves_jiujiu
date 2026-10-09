import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from .browser import Browser
from .data import load_product, validate
from .launcher import start_browser
from .media import manifest
from .ollama import Ollama
from .platform import Qianniu
from .runtime import ProcessLock, configuration_digest, configuration_files
from .store import Store
from .workflow import Workflow


ROOT = Path(__file__).resolve().parent.parent


def save(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parser():
    p = argparse.ArgumentParser(description="千牛录入、核对、入仓与恢复")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument('--expected-product-version')
    p.add_argument('--expected-config-digest')
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("menu")
    c.add_argument("product", nargs="?", default=str(ROOT / "products" / "jg-test.json"))
    for name in ("validate", "inspect", "fill", "submit", "resume", "audit-images", "enter-form", "map-detail-materials", "open-product-list", "open-warehouse-tab", "audit-warehouse", "plan-local-images"):
        c = commands.add_parser(name)
        c.add_argument("product")
        c.add_argument("--output")
    commands.add_parser("start-browser")
    for name in ('model-extract', 'model-suggest'):
        c = commands.add_parser(name)
        c.add_argument('product')
        c.add_argument('--output')
        if name == 'model-extract':
            c.add_argument('--file', required=True)
    for name in ('open-local-upload', 'open-upload-folder', 'set-upload-folder', 'upload-local-material'):
        c = commands.add_parser(name)
        c.add_argument('product')
        c.add_argument('--file', required=True)
        c.add_argument('--folder', required=True)
        c.add_argument('--output')
    c = commands.add_parser("reorder-details")
    c.add_argument("product")
    c.add_argument("--authorization", required=True)
    c.add_argument("--output")
    c = commands.add_parser("ack-manual-details")
    c.add_argument("product")
    c.add_argument("--statement", required=True)
    c.add_argument("--output")
    c = commands.add_parser('set-origin')
    c.add_argument('product')
    c.add_argument('--observed-current', required=True)
    c.add_argument('--output')
    for name in ("stage-sku", "open-sku-images", "browse-sku-materials", "preview-sku-image", "commit-sku-name", "open-sku-batch", "fill-sku-batch", "open-sku-area", "browse-detail-materials"):
        c = commands.add_parser(name)
        c.add_argument("product")
        c.add_argument("--output")
    for name in ("open-panel", "close-panel"):
        c = commands.add_parser(name)
        c.add_argument("product")
        c.add_argument("panel", choices=["specification", "materials", "logistics"])
        c.add_argument("--output")
    c = commands.add_parser("probe")
    c.add_argument("--timeout", type=int, default=0)
    c = commands.add_parser("model-probe")
    c.add_argument("--image")
    for name in ("report", "pause"):
        c = commands.add_parser(name)
        c.add_argument("task_id")
        c.add_argument("--output")
    c = commands.add_parser("resolve-submission")
    c.add_argument("task_id")
    c.add_argument("--operator", required=True)
    c.add_argument("--conclusion", choices=["continue_query", "candidate", "terminate", "not_submitted"], required=True)
    c.add_argument("--evidence", nargs="+", required=True)
    c.add_argument("--authorization", default="")
    c.add_argument("--item-id")
    return p


def menu(args):
    product = Path(args.product).resolve()
    actions = {"1": "validate", "2": "start-browser", "3": "probe", "4": "inspect",
               "5": "fill", "6": "submit", "7": "resume", "8": "report", "9": "model-probe"}
    labels = {"data_ready": "资料预检通过，尚未核对平台", "pending_data": "资料待补，未提交",
              "verified": "填写与全面回读通过，未提交", "confirmed_warehouse": "入仓与两处回查通过",
              "submission_unknown": "提交结果未知，只能回查", "paused": "已暂停，未确认入仓",
              "shop_observed": "已读到正确店铺", "waiting_user_login": "等待手动登录",
              "browser_started": "已打开专用浏览器", "browser_already_running": "专用浏览器已打开"}
    while True:
        print("\n千牛商品录入\n商品资料：" + str(product))
        print("1 资料校验   2 打开浏览器   3 登录检查   4 只读核对")
        print("5 填写并核对   6 单次提交入仓   7 恢复   8 查看记录   9 模型复测")
        print("P 更换资料   0 退出\n当前JG配置仅为测试；现场适配未验收时提交被阻止。")
        try:
            choice = input("选择：").strip().upper()
        except EOFError:
            return 0
        if choice == "0":
            return 0
        if choice == "P":
            product = Path(input("商品资料JSON路径：").strip().strip('"')).resolve()
            continue
        if choice not in actions:
            continue
        command = actions[choice]
        run_args = argparse.Namespace(config=args.config, command=command, product=str(product),
                                      output=None, timeout=0, image=None)
        if command == "report":
            try:
                run_args.task_id = load_product(product).task_id
            except Exception as e:
                print("资料无法读取：" + str(e))
                continue
        code, result = invoke(run_args)
        state = result.get("state")
        print(labels.get(state, "核对完整" if result.get("complete") else "操作结束，结果见记录"))
        if result.get("item_id"):
            print("新商品编号：" + result["item_id"])
        if result.get("issues"):
            print("待补/提示项：" + str(len(result["issues"])))
        if result.get("blockers") or code:
            print("存在阻塞，禁止未核对提交。详情保存在项目的运行记录目录。")


def invoke(args):
    root = Path(args.config).resolve().parent
    try:
        if args.command == "pause":
            save(root / "运行记录" / "STOP", {"task_id": args.task_id, "time": time.time()})
            return 0, {"state": "pause_requested", "task_id": args.task_id,
                       "reason": "next browser action will stop; already dispatched submission remains unknown"}
        with ProcessLock(root / "运行记录" / "run.lock"):
            result, code = execute(args)
            return code, result
    except KeyboardInterrupt:
        return 130, {"state": "paused", "reason": "operator interrupted; next run must reread platform"}
    except Exception as e:
        result = {"state": "paused", "reason": str(e), "time": time.time()}
        save(root / "运行记录" / "last-error.json", result)
        return 3, result


def execute(args):
    config_path = Path(args.config).resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    expected_config = getattr(args, 'expected_config_digest', None)
    if expected_config and configuration_digest(config_path) != expected_config:
        raise ValueError('runtime configuration version changed; no browser operation started')
    guarded_product = None
    expected_files = configuration_files(config_path) if expected_config else {}
    if getattr(args, 'expected_product_version', None):
        product_path = Path(args.product).resolve()
        expected_files[str(product_path)] = hashlib.sha256(product_path.read_bytes()).hexdigest()
        guarded_product = load_product(product_path)
        if guarded_product.version != args.expected_product_version:
            raise ValueError('product version changed; refresh preparation before browser operations')
    if args.command == "start-browser":
        return start_browser(config["browser"], root), 0
    if args.command == "probe":
        from .probe import run
        r = run(config_path, args.timeout)
        return r, 0 if r["state"] == "shop_observed" else 3
    if args.command == "model-probe":
        model = Ollama(config["ollama"])
        r = model.probe()
        if args.image:
            r["extraction"] = model.extract(args.image)
            r["visual_response_verified"] = True
        save(root / "运行记录" / "ollama-probe.json", r)
        return r, 0
    store = Store(root / config["state"])
    try:
        if args.command in ("report", "pause", "resolve-submission"):
            if args.command == "resolve-submission":
                store.resolve_submission(args.task_id, args.operator, args.conclusion, args.evidence,
                                         args.authorization, args.item_id)
            elif args.command == "pause":
                state = "submission_unknown" if store.intent(args.task_id) else "paused"
                store.state(args.task_id, state, {"reason": "operator requested pause"})
            return store.report(args.task_id), 0
        product = guarded_product or load_product(args.product)
        store.register(product)
        media = manifest(product)
        issues = [i.to_dict() for i in validate(product)] + media["issues"]
        report_dir = root / "运行记录" / product.task_id
        save(report_dir / "media-manifest.json", media)
        validation = {"state": "pending_data" if any(i.get("blocking", True) for i in issues) else "data_ready",
                      "task_id": product.task_id, "version": product.version, "issues": issues,
                      "known_paths": product.known_paths(), "time": time.time()}
        save(report_dir / "validation.json", validation)
        store.event(product.task_id, "validation", validation)
        if args.command == "validate":
            return validation, 2 if validation["state"] == "pending_data" else 0
        if args.command == 'plan-local-images':
            from .local_images import upload_plan
            result = upload_plan(product)
            save(report_dir / 'local-image-plan.json', result)
            store.event(product.task_id, 'local_image_plan', result)
            return result, 0 if result['ready'] else 2
        if args.command in ('model-extract', 'model-suggest'):
            model = Ollama(config['ollama'])
            if args.command == 'model-extract':
                if args.file not in media['files'] or 'error' in media['files'][args.file]:
                    raise ValueError('select an available configured product image')
                image = (Path(media['root']) / args.file).resolve()
                if not image.is_relative_to(Path(media['root']).resolve()):
                    raise ValueError('image escapes product root')
                from .media import ensure_version
                ensure_version(product)
                advice = model.extract(image)
                ensure_version(product)
            else:
                advice = model.suggest(product.data.get('title_facts', []))
            from .data import title_units
            if args.command == 'model-suggest':
                forbidden = [product.value('fields.brand')] + product.data.get('forbidden_title_terms', [])
                advice['title_count'] = title_units(advice['title'])
                advice['guide_title_count'] = title_units(advice['guide_title'])
                advice['policy_ok'] = advice['title_count'] == 60 and advice['guide_title_count'] == 30 and not any(
                    term and term.casefold() in (advice['title'] + advice['guide_title']).casefold() for term in forbidden)
                gaps = list(advice.get('missing', [])) if isinstance(advice.get('missing'), list) else []
                if advice['title_count'] != 60:
                    gaps.append(f"宝贝标题计数{advice['title_count']}/60；须人工修订，不能堆词补齐")
                if advice['guide_title_count'] != 30:
                    gaps.append(f"导购标题计数{advice['guide_title_count']}/30；须人工修订")
                if any(term and term.casefold() in (advice['title'] + advice['guide_title']).casefold() for term in forbidden):
                    gaps.append('标题含品牌或商标，不可采用')
                advice['missing'] = gaps
            result = {'state': 'model_advice_ready', 'action': args.command, 'version': product.version,
                      'model': model.model if hasattr(model, 'model') else config['ollama']['model'],
                      'advice': advice, 'confirmed': False, 'submission_authorized': False,
                      'time': time.time()}
            save(report_dir / 'model-advice.json', result)
            store.event(product.task_id, 'model_advice', result)
            return result, 0
        profile = json.loads((root / config["profile"]).read_text(encoding="utf-8-sig"))
        browser_config = dict(config["browser"])
        browser_config['expected_files'] = expected_files
        browser_config["profile_dir"] = str((root / browser_config["profile_dir"]).resolve())
        browser_config["stop_file"] = str(root / "运行记录" / "STOP")
        if args.command in ("fill", "submit", "resume"):
            stop = Path(browser_config["stop_file"])
            if stop.exists():
                stop.unlink()
                store.event(product.task_id, "operator_resume", {"command": args.command})
        if expected_config and configuration_digest(config_path) != expected_config:
            raise ValueError('runtime configuration changed before browser connection')
        with Browser(browser_config, report_dir / "observations") as browser:
            platform = Qianniu(browser, profile, store)
            if args.command == 'audit-warehouse':
                result = platform.run_phase(product, platform.audit_warehouse, product)
                if args.output:
                    save(args.output, result)
                return result, 0
            if args.command in ('open-product-list', 'open-warehouse-tab'):
                result = platform.run_phase(product, platform.open_product_list, product, warehouse=args.command == 'open-warehouse-tab')
                save(report_dir / 'product-list-latest.json', result)
                return result, 0
            if args.command == 'map-detail-materials':
                def map_fresh():
                    records = [e for e in store.report(product.task_id)['events'] if e['kind'] == 'sku_material_folder'
                               and json.loads(e['payload']).get('folder') == product.data['media']['folders']['detail']]
                    if not records or time.time() - records[-1]['created'] > 600:
                        from .browser import PlatformPause
                        raise PlatformPause('fresh actual detail material listing required')
                    metadata = json.loads(records[-1]['payload'])
                    if metadata.get('source') != browser.source:
                        raise ValueError('material listing source differs')
                    result = platform.map_detail_materials(product, platform.audit_detail_preview(product), metadata)
                    store.event(product.task_id, 'detail_material_mapping', result)
                    return result
                result = platform.run_phase(product, map_fresh)
                save(report_dir / 'detail-material-mapping.json', result)
                return result, 0
            if args.command == 'browse-detail-materials':
                result = platform.run_phase(product, platform.browse_sku_materials, product, kind='detail')
                save(report_dir / 'detail-materials-latest.json', result)
                return result, 0
            if args.command in ('open-local-upload', 'open-upload-folder', 'set-upload-folder', 'upload-local-material'):
                action = {'open-local-upload': platform.open_local_upload, 'open-upload-folder': platform.open_upload_folder,
                          'set-upload-folder': platform.set_upload_folder, 'upload-local-material': platform.upload_local_material}[args.command]
                result = platform.run_phase(product, action, product, args.file, args.folder)
                save(report_dir / 'upload-panel-latest.json', result)
                return result, 0
            if args.command == 'set-origin':
                result = platform.run_phase(product, platform.set_origin, product, args.observed_current)
                save(report_dir / 'origin-latest.json', result)
                return result, 0
            if args.command == "ack-manual-details":
                result = platform.run_phase(product, platform.ack_manual_details, product, args.statement)
                save(report_dir / "manual-detail-order.json", result)
                return result, 0
            if args.command == 'fill-sku-batch':
                platform.run_phase(product, platform.fill_batch, product, product.known_paths())
                result = {'state': 'sku_batch_readback', **platform.read_all_skus(), 'submitted': False}
                save(report_dir / 'sku-batch-latest.json', result)
                return result, 0
            if args.command == 'open-sku-area':
                result = platform.run_phase(product, platform.open_sku_batch, product, area=True)
                save(report_dir / 'sku-area-latest.json', result)
                return result, 0
            if args.command in ("stage-sku", "open-sku-images", "browse-sku-materials", "preview-sku-image", "commit-sku-name", "open-sku-batch"):
                action = {"stage-sku": platform.stage_single_sku, "open-sku-images": platform.open_sku_images,
                          "browse-sku-materials": platform.browse_sku_materials, "preview-sku-image": platform.open_sku_preview,
                          "commit-sku-name": platform.commit_single_sku_name, "open-sku-batch": platform.open_sku_batch}[args.command]
                result = platform.run_phase(product, action, product)
                save(report_dir / "sku-stage-latest.json", result)
                return result, 0
            if args.command == "reorder-details":
                result = platform.reorder_details(product, args.authorization)
                save(report_dir / "detail-sort-latest.json", result)
                return result, 0
            if args.command == "enter-form":
                result = platform.ensure_form(product)
                save(report_dir / "form-entry-latest.json", result)
                return result, 0
            if args.command in ("open-panel", "close-panel"):
                action = platform.open_panel if args.command == 'open-panel' else platform.close_panel
                result = platform.run_phase(product, action, product, args.panel)
                save(report_dir / "panel-latest.json", result)
                return result, 0
            if args.command == "audit-images":
                result = platform.audit_detail_preview(product)
                save(report_dir / "detail-preview-audit.json", result)
                store.event(product.task_id, "detail_preview_audit", result)
                if not result["content_order_ok"]:
                    store.state(product.task_id, "paused", {"reason": "strict detail content/order correspondence not established; possible platform conversion or different material", "audit": result})
                return result, 0 if result["ok"] else 3
            if args.command == "inspect":
                observation = platform.read_form(product)
                from dataclasses import asdict
                from .workflow import compare
                checks = compare(product, observation)
                store.observe(product, checks['checks'], observation.source, observation.evidence, observation.observed)
                if not checks['ok']:
                    store.state(product.task_id, 'submission_unknown' if store.intent(product.task_id) else 'paused',
                                {'reason': 'full readback incomplete', 'blockers': checks['blockers']})
                result = asdict(observation)
                result['checks'] = checks['checks']
                save(report_dir / "inspect-latest.json", result)
                return result, 0 if checks['ok'] else 3
            workflow = Workflow(store, platform)
            result = workflow.run(product, submit=args.command == "submit")
            save(report_dir / "result-latest.json", result)
            save(report_dir / "report.json", store.report(product.task_id))
            return result, 0 if result["state"] in ("verified", "confirmed_warehouse") else 3
    finally:
        store.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure') and not stream.isatty():
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    if args.command == "menu":
        return menu(args)
    code, result = invoke(args)
    if getattr(args, "output", None):
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
