SPEC = {"name": "weather", "description": "查询城市天气", "parameters": {"city": "城市名"}}
def run(city="深圳"):
    import urllib.request
    url = f"https://wttr.in/{city}?format=j1"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    cur = d["current_condition"][0]
    return f"{city}: {cur['lang_zh'][0]['value']}, {cur['temp_C']}°C, 体感{cur['FeelsLikeC']}°C, 湿度{cur['humidity']}%"
import json
