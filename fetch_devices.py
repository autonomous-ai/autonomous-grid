import json, urllib.request, urllib.parse

UA = {"User-Agent": "grid-readme/1.0 (https://github.com/autonomous-ai/autonomous-grid; contact dee@autonomous.ai)"}
QUERIES = {
    "mac-studio": "Mac Studio",
    "mac-mini": "Mac mini",
    "macbook-pro": "MacBook Pro",
    "rtx-gpu": "GeForce RTX graphics card",
}


def search(q):
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "generator": "search",
        "gsrsearch": f"{q}", "gsrnamespace": "6", "gsrlimit": "10",
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "600",
        "format": "json",
    })
    req = urllib.request.Request(url, headers=UA)
    data = json.load(urllib.request.urlopen(req, timeout=30))
    out = []
    for p in data.get("query", {}).get("pages", {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime", "").startswith("image/") and ii.get("thumburl"):
            out.append((p["title"], ii.get("width"), ii.get("height"), ii["thumburl"]))
    return out


for key, q in QUERIES.items():
    print(f"\n## {key}  ({q})")
    try:
        for t, w, h, u in search(q)[:10]:
            print(f"  {w}x{h}  {t}\n     {u}")
    except Exception as e:
        print("  ERROR", e)
