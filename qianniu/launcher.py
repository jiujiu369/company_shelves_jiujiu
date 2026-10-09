import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .browser import PlatformPause


def start_browser(config, root):
    endpoint = config.get("cdp_url", "http://127.0.0.1:9223")
    parsed = urlsplit(endpoint)
    if parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port:
        raise PlatformPause("managed browser needs an explicit loopback port")
    profile = (Path(root) / config["profile_dir"]).resolve()
    candidates = [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
                  Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"]
    executable = next((p for p in candidates if p.exists()), None)
    if not executable:
        raise PlatformPause("Chrome executable not found; set up Chrome before launching")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint + "/json/version", timeout=1) as r:
            data = json.load(r)
        return {"state": "browser_already_running", "endpoint": endpoint, "browser": data.get("Browser")}
    except OSError:
        pass
    profile.mkdir(parents=True, exist_ok=True)
    args = [str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={parsed.port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--new-window",
            config.get("start_url", "https://qn.taobao.com/home.htm")]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with opener.open(endpoint + "/json/version", timeout=1) as r:
                data = json.load(r)
            return {"state": "browser_started", "endpoint": endpoint, "pid": process.pid,
                    "profile_dir": str(profile), "browser": data.get("Browser"), "auto_close": False}
        except OSError:
            time.sleep(0.3)
    raise PlatformPause("browser startup not observed; check current window before retry")
