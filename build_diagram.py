"""Build a rendered PNG of the Grid diagram with real logos baked in.

Processes the OpenClaw + Hermes logos (transparent bg, trimmed, downscaled),
inlines them as <image> into docs/home-grid.svg, and writes build/render.html
for headless Chrome to screenshot.
"""
import base64, io
from PIL import Image

LOGOS = "docs/logos"


def autocrop(im):
    bbox = im.getbbox()
    return im.crop(bbox) if bbox else im


def downscale(im, target_w):
    w, h = im.size
    if w <= target_w:
        return im
    nh = int(round(h * target_w / w))
    return im.resize((target_w, nh), Image.LANCZOS)


def proc_hermes():
    im = Image.open(f"{LOGOS}/hermes.png").convert("RGBA")
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r < 45 and g < 45 and b < 45:        # near-black background -> transparent
                px[x, y] = (r, g, b, 0)
    return downscale(autocrop(im), 460)


def proc_openclaw():
    im = Image.open(f"{LOGOS}/openclaw.png").convert("RGBA")
    return downscale(autocrop(im), 520)


def b64(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def img_tag(data, iw, ih, cx, cy=95, maxw=152, maxh=42):
    scale = min(maxw / iw, maxh / ih)
    w, h = iw * scale, ih * scale
    x, y = cx - w / 2, cy - h / 2
    return (f'<image x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{data}"/>')


hermes, oc = proc_hermes(), proc_openclaw()
hw, hh = hermes.size
ow, oh = oc.size

svg = open("docs/home-grid.svg").read()
svg = svg.replace('viewBox="0 0 1100 726"', 'viewBox="0 0 1100 726" width="1100" height="726"', 1)
svg = svg.replace('<text x="280" y="101" fill="#ffffff" font-size="17" font-weight="700">OpenClaw</text>', '')
svg = svg.replace('<text x="550" y="101" fill="#ffffff" font-size="17" font-weight="700">Hermes</text>', '')
svg = svg.replace('</svg>', img_tag(b64(oc), ow, oh, 280) + '\n' + img_tag(b64(hermes), hw, hh, 550) + '\n</svg>')

html = ('<!doctype html><html><head><meta charset="utf-8">'
        '<style>html,body{margin:0;padding:0}</style></head><body>' + svg + '</body></html>')
open("build/render.html", "w").write(html)
print(f"built: openclaw {ow}x{oh}, hermes {hw}x{hh}")
