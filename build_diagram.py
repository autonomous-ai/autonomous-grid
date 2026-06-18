"""Generate the Grid diagram and render it to docs/home-grid.png via headless Chrome.

Real OpenClaw/Hermes logos (base64) sit in the app cards; real device photos
(docs/devices/*) sit in white thumbnails beside each engine card. GitHub can't
render raster inside an SVG, so we screenshot to a PNG.
"""
import base64, io, os
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
W, H = 1140, 648


# ---------- asset processing ----------
def autocrop(im):
    bb = im.getbbox()
    return im.crop(bb) if bb else im


def downscale(im, tw):
    w, h = im.size
    return im if w <= tw else im.resize((tw, int(round(h * tw / w))), Image.LANCZOS)


def b64_png(im):
    buf = io.BytesIO(); im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def hermes_logo():
    im = Image.open(f"{ROOT}/docs/logos/hermes.png").convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if r < 45 and g < 45 and b < 45:
                px[x, y] = (r, g, b, 0)
    return downscale(autocrop(im), 460)


def openclaw_logo():
    return downscale(autocrop(Image.open(f"{ROOT}/docs/logos/openclaw.png").convert("RGBA")), 520)


OC = b64_png(openclaw_logo())
HE = b64_png(hermes_logo())


def fileuri(p):
    return "file://" + os.path.join(ROOT, p)


# ---------- svg helpers ----------
def logo_img(data, cx, cy, maxw, maxh, iw, ih):
    s = min(maxw / iw, maxh / ih); w, h = iw * s, ih * s
    return (f'<image x="{cx-w/2:.1f}" y="{cy-h/2:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'preserveAspectRatio="xMidYMid meet" href="{data}"/>')


def app_card(x, label, logo=None, isz=None):
    cx = x + 100
    g = f'<g filter="url(#sh)"><rect x="{x}" y="58" width="200" height="58" rx="15" fill="url(#appGrad)"/></g>'
    g += f'<rect x="{x+6}" y="63" width="188" height="20" rx="10" fill="#ffffff" opacity="0.10"/>'
    if logo:
        g += logo_img(logo, cx, 89, 150, 40, *isz)
    else:
        g += f'<text x="{cx}" y="95" text-anchor="middle" fill="#ffffff" font-size="17" font-weight="700">{label}</text>'
    return g


def engine_unit(x, name, fw, hw, vram, photo, transparent):
    """Spec card at y=388 with a device photo thumbnail below it."""
    cw, ch, cy = 180, 92, 388
    tx = x + 14
    card = f'<g filter="url(#sh)"><rect x="{x}" y="{cy}" width="{cw}" height="{ch}" rx="14" fill="url(#engineGrad)"/></g>'
    card += f'<rect x="{x+6}" y="{cy+5}" width="{cw-12}" height="22" rx="11" fill="#ffffff" opacity="0.10"/>'
    card += f'<text x="{tx}" y="{cy+25}" fill="#ffffff" font-size="14.5" font-weight="700">{name}</text>'
    rows = [("FRAMEWORK", fw), ("HARDWARE", hw), ("VRAM", vram)]
    for i, (lab, val) in enumerate(rows):
        yy = cy + 47 + i * 17
        card += (f'<text x="{tx}" y="{yy}"><tspan fill="#bcd9ef" font-size="9.5" letter-spacing="0.5">{lab}</tspan>'
                 f'<tspan fill="#ffffff" font-weight="700" font-size="11.5">  {val}</tspan></text>')
    # photo thumbnail below
    py, phh = cy + ch + 12, 84
    frame = f'<g filter="url(#sh)"><rect x="{x}" y="{py}" width="{cw}" height="{phh}" rx="13" fill="#ffffff"/></g>'
    pad = 9
    frame += (f'<image x="{x+pad}" y="{py+pad}" width="{cw-2*pad}" height="{phh-2*pad}" '
              f'preserveAspectRatio="xMidYMid meet" href="{fileuri(photo)}"/>')
    return card + frame


ENGINES = [
    ("Main Desktop", "MLX", "Mac Studio", "512 GB", "docs/devices/mac-studio.jpg", False),
    ("Home Server", "Ollama", "Mac mini", "64 GB", "docs/devices/mac-mini.jpg", False),
    ("Laptop", "LM Studio", "MacBook Pro", "36 GB", "docs/devices/macbook-pro.jpg", False),
    ("Basement Rig", "vLLM", "RTX 3090", "24 GB", "docs/devices/gpu.png", True),
    ("Gaming PC", "llama.cpp", "RTX 4090", "24 GB", "docs/devices/gpu.png", True),
    ("Render Box", "ComfyUI", "RTX 5090", "32 GB", "docs/devices/gpu.png", True),
]
APPS_X = [190, 470, 750]
ENGINE_X = [5, 195, 385, 575, 765, 955]

oc_im, he_im = openclaw_logo(), hermes_logo()

defs = '''<defs>
  <radialGradient id="bgGrad" cx="0.5" cy="0.30" r="0.95"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#eceff4"/></radialGradient>
  <pattern id="dots" width="26" height="26" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.4" fill="#d7dce4"/></pattern>
  <linearGradient id="engineGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5fb0e6"/><stop offset="1" stop-color="#3a8cc8"/></linearGradient>
  <linearGradient id="appGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8b8ff2"/><stop offset="1" stop-color="#6366f1"/></linearGradient>
  <linearGradient id="gridGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#34d399"/><stop offset="1" stop-color="#0ea371"/></linearGradient>
  <linearGradient id="lineEG" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#3f95d4"/><stop offset="1" stop-color="#10b981"/></linearGradient>
  <linearGradient id="lineGA" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#10b981"/><stop offset="1" stop-color="#6366f1"/></linearGradient>
  <marker id="aG" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,3 L0,6 Z" fill="#10b981"/></marker>
  <marker id="aI" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,3 L0,6 Z" fill="#6366f1"/></marker>
  <filter id="sh" x="-30%" y="-50%" width="160%" height="200%"><feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#1e293b" flood-opacity="0.20"/></filter>
  <filter id="glow" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="22"/></filter>
</defs>'''

parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         'font-family="ui-sans-serif, -apple-system, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif">',
         defs,
         f'<rect width="{W}" height="{H}" fill="url(#bgGrad)"/><rect width="{W}" height="{H}" fill="url(#dots)"/>',
         '<text x="570" y="40" text-anchor="middle" fill="#8a96a8" font-size="12" letter-spacing="2.5" font-weight="700">APPS</text>']

# arrows grid -> apps
for cx in [x + 100 for x in APPS_X]:
    parts.append(f'<path d="M{cx} 196 C{cx+22} 162, {cx+22} 162, {cx} 120" fill="none" stroke="url(#lineGA)" stroke-width="2.6" opacity="0.9" marker-end="url(#aI)"/>')
# arrows engine -> grid
for x in ENGINE_X:
    cx = x + 90
    parts.append(f'<path d="M{cx} 388 C{cx} 344, {cx} 344, {cx} 290" fill="none" stroke="url(#lineEG)" stroke-width="2.6" opacity="0.9" marker-end="url(#aG)"/>')

# grid band
parts.append('<ellipse cx="570" cy="244" rx="450" ry="66" fill="#10b981" opacity="0.18" filter="url(#glow)"/>')
parts.append('<g filter="url(#sh)"><rect x="90" y="196" width="960" height="92" rx="20" fill="url(#gridGrad)"/></g>')
parts.append('<rect x="98" y="202" width="944" height="30" rx="15" fill="#ffffff" opacity="0.10"/>')
parts.append('<text x="570" y="240" text-anchor="middle" fill="#ffffff" font-size="26" font-weight="800">Your Home Grid</text>')
parts.append('<text x="570" y="269" text-anchor="middle" fill="#f3fff9" font-size="18.5">Unifies and orchestrates all your compute resources</text>')

# apps
parts.append(app_card(APPS_X[0], "OpenClaw", OC, oc_im.size))
parts.append(app_card(APPS_X[1], "Hermes", HE, he_im.size))
parts.append(app_card(APPS_X[2], "Your Own App"))

# engines
for x, e in zip(ENGINE_X, ENGINES):
    parts.append(engine_unit(x, *e))

parts.append('<text x="570" y="624" text-anchor="middle" fill="#5b6b85" font-size="13.5" letter-spacing="2.5" font-weight="700">INFERENCE ENGINES</text>')
parts.append('<text x="570" y="642" text-anchor="middle" fill="#5b6b85" font-size="14">Your machines, wired into your home grid</text>')
parts.append('</svg>')

svg = "\n".join(parts)
os.makedirs(f"{ROOT}/build", exist_ok=True)
open(f"{ROOT}/build/render.html", "w").write(
    '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0}</style></head><body>' + svg + '</body></html>')
print("built render.html")
