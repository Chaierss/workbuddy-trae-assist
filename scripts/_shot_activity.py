"""活动页截图。后端已在 57910 跑着的话复用，否则这里只读接口。"""

import importlib.util
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:57910"
OUT = "docs/_shot-activity.png"


def fetch(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    data = fetch("/api/activities?refresh=true")
    print("summary:", json.dumps(data["summary"], ensure_ascii=False))
    for account in data["accounts"]:
        print(f"- {account['displayName']} [{account['platform']}] detectable={account['detectable']}")
        for item in account["activities"]:
            print(f"    {item['key']:26} {item['state']:12} auto={item['automatable']} | {item['rewardText']} | {item['progressText']} | {item['note']}")


if __name__ == "__main__":
    main()
