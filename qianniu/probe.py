"""Read-only browser/session diagnostics; never fills or submits a product."""
import argparse
import json
import time
from pathlib import Path

from .browser import Browser, PlatformPause


def run(config_path="config.json", timeout=0):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    bconfig = dict(config["browser"])
    bconfig["profile_dir"] = str((path.parent / bconfig["profile_dir"]).resolve())
    report_dir = path.parent / "运行记录" / "browser-feasibility"
    with Browser(bconfig, report_dir) as browser:
        if browser.page.url == "about:blank":
            browser.goto(bconfig["start_url"])
        print(json.dumps({"state": "browser_open", "evidence": browser.capture("initial")},
                         ensure_ascii=False), flush=True)
        deadline = time.monotonic() + timeout
        while True:
            snapshot = browser.snapshot()
            shop = "温州市施科电器厂"
            visible_text = " ".join(f.get("dom", {}).get("text", "") for f in snapshot["frames"])
            try:
                browser.guard()
                signed_in = shop in visible_text
                reason = "expected shop observed; category/form feasibility still pending" if signed_in else "expected shop not yet observed"
            except PlatformPause as e:
                signed_in, reason = False, str(e)
            if signed_in or time.monotonic() >= deadline:
                result = {"state": "shop_observed" if signed_in else "waiting_user_login",
                          "shop": shop if signed_in else None, "reason": reason,
                          "evidence": browser.capture("signed-in" if signed_in else "login-stop"),
                          "time": time.time()}
                target = report_dir / "probe-latest.json"
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result
            browser.page.wait_for_timeout(500)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--timeout", type=int, default=0)
    args = parser.parse_args()
    run(args.config, args.timeout)
