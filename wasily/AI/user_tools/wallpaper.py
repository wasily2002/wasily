SPEC = {"name": "wallpaper", "description": "下载必应每日壁纸并设置为桌面背景", "parameters": {}}
def run():
    import ctypes, json, urllib.request
    with urllib.request.urlopen("https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1", timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    url = "https://www.bing.com" + d["images"][0]["url"]
    dst = os.path.join(os.path.expanduser("~"), "Pictures", "bing_daily.jpg")
    urllib.request.urlretrieve(url, dst)
    ctypes.windll.user32.SystemParametersInfoW(20, 0, dst, 3)
    return f"壁纸已更换: {d['images'][0].get('copyright', '')}"
import os
