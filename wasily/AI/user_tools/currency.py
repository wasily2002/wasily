SPEC = {"name": "currency", "description": "汇率换算", "parameters": {"amount": "金额", "src": "源币种如USD", "dst": "目标币种如CNY"}}
def run(amount=100, src="USD", dst="CNY"):
    import urllib.request
    url = f"https://api.frankfurter.app/latest?amount={amount}&from={src}&to={dst}"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    return f"{amount} {src} = {d['rates'][dst]} {dst}（{d['date']}）"
import json
