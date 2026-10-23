"""CPU-only pixel recognizer for a visible plus (+) upload icon.

Uses a screenshot from the owner's SAME Kaggle Chrome. No API key, GPU, new
browser, remote image upload or external vision service. The result is only
a candidate: visual_ui.click_grounded verifies it against the live DOM.
"""
from __future__ import annotations

from io import BytesIO
from statistics import median
from typing import Any

def plus_confidence(image: Any, x: float, y: float) -> float:
    """Identify two intersecting, straight arms centered near a point.

    Work in local monochrome pixel contrasts so light/dark Gemini themes work.
    Return 0..1. Evaluates only the center of an actual interactive element,
    NOT arbitrary screen coordinates.
    """
    im = image.convert("L")
    w, h = im.size
    x0, y0 = round(x), round(y)
    if x0 < 16 or y0 < 16 or x0 >= w-16 or y0 >= h-16:
        return 0.0
    border = []
    for d in range(-15,16,3):
        border.extend([im.getpixel((x0+d,y0-15)), im.getpixel((x0+d,y0+15)),
                       im.getpixel((x0-15,y0+d)), im.getpixel((x0+15,y0+d))])
    bg = float(median(border))
    best = 0.0
    # Icons vary in color, thickness and a few pixels of alignment.
    for sign in (-1,1):
        for dy in range(-5,6,2):
            for dx in range(-5,6,2):
                cx,cy = x0+dx,y0+dy
                def active(px,py):
                    return (im.getpixel((px,py))-bg)*sign > 48
                def pixel_h(px):
                    return any(active(px,yy) for yy in (cy-1,cy,cy+1))
                def pixel_v(py):
                    return any(active(xx,py) for xx in (cx-1,cx,cx+1))
                left  = sum(pixel_h(cx-d) for d in range(3,12))/9.0
                right = sum(pixel_h(cx+d) for d in range(3,12))/9.0
                up    = sum(pixel_v(cy-d) for d in range(3,12))/9.0
                down  = sum(pixel_v(cy+d) for d in range(3,12))/9.0
                middle = sum(active(xx,yy) for xx in (cx-1,cx,cx+1)
                              for yy in (cy-1,cy,cy+1))/9.0
                strength = min(left,right,up,down) * min(1.0,middle*1.5)
                if strength>best:
                    best=strength
    return best


def locate_visible_plus(page, observation: dict) -> tuple[float,float] | None:
    """Inspect real pixels of observed composer-area controls, without a key."""
    try:
        from PIL import Image
    except ImportError:
        print("TOOL_SYSTEM_VISUAL_LOCAL=DEPENDENCY_PILLOW_MISSING",flush=True)
        return None
    from .visual_ui import _screenshot_clip
    clip = _screenshot_clip(observation)
    # Playwright's CSS scale keeps observed DOM centers pixel-aligned.
    try:
        shot = page.screenshot(type="png", clip=clip, scale="css", timeout=15000)
        image = Image.open(BytesIO(shot)).convert("RGB")
    except Exception:
        print("TOOL_SYSTEM_VISUAL_LOCAL=SCREENSHOT_FAILED",flush=True)
        return None
    examined=[]
    for control in observation.get("buttons",[])[:45]:
        if float(control.get("width",0)) > 90 or float(control.get("height",0)) > 90:
            continue
        px = float(control.get("x",-1)) - clip["x"]
        py = float(control.get("y",-1)) - clip["y"]
        if px<17 or py<17 or px>image.width-17 or py>image.height-17:
            continue
        from .visual_ui import _NO
        if _NO.search(str(control.get("hint",""))):
            continue
        confidence = plus_confidence(image,px,py)
        if confidence >= 0.41:
            examined.append((confidence,control))
    examined.sort(key=lambda c:c[0],reverse=True)
    # Nested SVG, icon wrapper and parent button often share the same icon
    # center. Do not count those as multiple ambiguous + buttons.
    unique=[]
    for strength,item in examined:
        if all(abs(float(item["x"])-float(other["x"]))>9 or
               abs(float(item["y"])-float(other["y"]))>9
               for _,other in unique):
            unique.append((strength,item))
    if not unique:
        print("TOOL_SYSTEM_VISUAL_LOCAL=PLUS_ICON_NOT_FOUND",flush=True)
        return None
    if len(unique)>1 and unique[0][0]-unique[1][0] < 0.13:
        print("TOOL_SYSTEM_VISUAL_LOCAL=AMBIGUOUS_PLUS_ICONS",flush=True)
        return None
    item = unique[0][1]
    print("TOOL_SYSTEM_VISUAL_LOCAL=PIXEL_PLUS_GROUNDED",flush=True)
    return float(item["x"]),float(item["y"])
