"""号池页截图（含多选批量条）。"""
import base64
import importlib.util
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cdp_shot", r"D:/DBRoad/MyPython/workbuddy-trae-assist/scripts/cdp-shot.py"
)
cdp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdp)

URL = "http://127.0.0.1:57910/accounts"
OUT = Path(r"D:/DBRoad/MyPython/chaierww/docs/_shot-pool-credits.png")

s = cdp._Session()
try:
    s.new_page(URL)
    s.call("Page.enable")
    s.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 2, "mobile": False})
    s.call("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 255, "g": 255, "b": 255, "a": 1}})
    s.call("Page.navigate", {"url": URL})
    time.sleep(3.5)
    # 勾选第一个账号，让批量条出现
    s.call("Runtime.evaluate", {
        "expression": "document.querySelector('button[aria-label=\"选择该账号\"]')?.click()",
        "returnByValue": True,
    })
    time.sleep(0.8)
    res = s.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
finally:
    s.close()

OUT.write_bytes(base64.b64decode(res["data"]))
from PIL import Image
with Image.open(OUT) as im:
    print(f"[shot] {OUT}  {im.size[0]}x{im.size[1]}")
