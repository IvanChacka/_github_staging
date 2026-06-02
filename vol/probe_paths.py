"""路径探测：确定哪些品种需要 _O 后缀"""
import requests
import time

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})

# 从 flow-data 取前50个不同品种的合约
r = s.post("https://www.openvlab.cn/api/flow-data", json={"page": 1, "pageSize": 50}, timeout=15)
contracts = r.json()["result"]["data"]

seen_products = {}
for c in contracts:
    und = c["product_und"]
    cc = c["contract_code"]
    instr = c["instrument"]
    if und in seen_products:
        continue
    
    # 解析
    try:
        prefix, month, opt_type, strike = instr.split(":")
        # 提取纯产品代码
        parts = prefix.split("_")
        product_code = parts[-1]  # 如 MO, AU, AG, J, JM
        
        # 试两种格式
        paths_to_try = [
            (f"{product_code}_O/{month}/{opt_type}/{strike}", f"{product_code}_O"),
            (f"{product_code}/{month}/{opt_type}/{strike}", product_code),
        ]
        
        for path, label in paths_to_try:
            r2 = s.post(f"https://www.openvlab.cn/api/option-series-with-underlying/{path}", json={}, timeout=15)
            if r2.status_code == 200:
                series = r2.json().get("result", {}).get("option_series", [])
                iv_count = sum(1 for row in series if row[7] is not None)
                print(f"OK {und:6s} ({cc:18s}) -> {label:15s} rows={len(series)} IV有={iv_count}")
                seen_products[und] = label
                break
        else:
            print(f"XX {und:6s} ({cc:18s}) -> 无可用路径")
            seen_products[und] = None
    except Exception as e:
        print(f"EE {und:6s} ({cc:18s}) -> {e}")
        seen_products[und] = None
    
    time.sleep(0.3)

print("\n\n=== 路径映射表 ===")
for und, path in sorted(seen_products.items()):
    print(f'  "{und}": "{path}"', end="")
    if path:
        print(",")
    else:
        print(" # 无可用的系列路径")
