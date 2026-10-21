"""打开号池页并点击「JSON 导入」按钮截图对话框。"""
import importlib.util
import time
from pathlib import Path
import json

spec = importlib.util.spec_from_file_location(
    "cdp_shot", r"D:/DBRoad/MyPython/workbuddy-trae-assist/scripts/cdp-shot.py"
)
cdp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdp)

URL = "http://127.0.0.1:57910/accounts"
OUT = Path(r"D:/DBRoad/MyPython/chaierww/docs/_shot-json-import.png")

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
    time.sleep(3.0)
    # 点击「JSON 导入」按钮
    js = """
    (() => {
      const btns = [...document.querySelectorAll('button')];
      const b = btns.find(x => x.textContent.trim() === 'JSON 导入');
      if (b) { b.click(); return 'clicked'; }
      return 'not-found';
    })()
    """
    s.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    time.sleep(1.5)
    res = s.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
finally:
    s.close()

import base64
OUT.write_bytes(base64.b64decode(res["data"]))
from PIL import Image
with Image.open(OUT) as im:
    print(f"[shot] {OUT}  {im.size[0]}x{im.size[1]}")
