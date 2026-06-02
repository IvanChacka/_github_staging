"""
探针：看看 flow-data API 到底返回哪些字段
"""
import json
import requests

resp = requests.post("https://www.openvlab.cn/api/flow-data", json={"page": 1, "pageSize": 3}, timeout=15)
data = resp.json()
print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
