"""给 chaierww 页面截图。"""
import importlib.util
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cdp_shot", r"D:/DBRoad/MyPython/workbuddy-trae-assist/scripts/cdp-shot.py"
)
cdp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdp)

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:57910/accounts"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else r"D:/DBRoad/MyPython/chaierww/docs/_shot.png")

s = cdp._Session()
try:
    s.new_page(URL)
    s.call("Page.enable")
    s.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1280, "height": 900, "deviceScaleFactor": 2, "mobile": False},
    )
    s.call(
        "Emulation.setDefaultBackgroundColorOverride",
        {"color": {"r": 255, "g": 255, "b": 255, "a": 1}},
    )
    s.call("Page.navigate", {"url": URL})
    time.sleep(3.5)
    res = s.call(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": False},
    )
finally:
    s.close()

import base64

OUT.write_bytes(base64.b64decode(res["data"]))
from PIL import Image

with Image.open(OUT) as im:
    print(f"[shot] {OUT}  {im.size[0]}x{im.size[1]}")
