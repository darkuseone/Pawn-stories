"""Четыре направления оформления шапки, рендер на настоящем ролике."""
import subprocess, sys
from pathlib import Path
from PIL import ImageFont

W, H = 1080, 1920
FONT_FILE = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT = "Liberation Sans"
SRC = Path("/tmp/ep08/final.mp4")
OUT = Path("/tmp/vartest")

QUESTION = "How much is a 1943 copper penny really worth?"
SUB = "Another, the finest known example from"


def wrap(text, per_line):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def measure(lines, size):
    f = ImageFont.truetype(FONT_FILE, size)
    return max(f.getlength(l) for l in lines), size * 1.30


def rrect(w, h, r):
    """Скруглённый прямоугольник в режиме рисования ASS, начало в 0,0."""
    w, h, r = round(w), round(h), round(r)
    return (f"m {r} 0 l {w-r} 0 b {w} 0 {w} 0 {w} {r} "
            f"l {w} {h-r} b {w} {h} {w} {h} {w-r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h-r} "
            f"l 0 {r} b 0 0 0 0 {r} 0")


def head(styles):
    return (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\n"
            "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginRight, MarginV, Encoding\n" + styles +
            "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
            "MarginR, MarginV, Effect, Text\n")


def sub_rows(size=70):
    lines = wrap(SUB, 20)
    txt = "\\N".join(lines)
    return (f"Dialogue: 5,0:00:00.00,0:00:20.00,SUB,,0,0,0,,"
            f"{{\\pos({W//2},{int(H*0.66)})}}{txt}\n")


SUB_STYLE = (f"Style: SUB,{FONT},70,&H00FFFFFF,&H00000000,&H80000000,"
             "-1,0,0,0,100,100,1,0,1,6,3,5,60,60,60,1\n")


def variant_glass():
    """A — стекло: тёмная полупрозрачная панель, тонкая светлая рамка."""
    size = 58
    lines = wrap(QUESTION, 22)
    tw, lh = measure(lines, size)
    pad_x, pad_y = 46, 34
    pw, ph = tw + 2 * pad_x, lh * len(lines) + 2 * pad_y
    x, y = (W - pw) / 2, 70
    st = (f"Style: T,{FONT},{size},&H0000D4FF,&H00000000,&H00000000,"
          "-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
          f"Style: PANEL,{FONT},60,&H00FFFFFF,&H00FFFFFF,&H00000000,"
          "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n" + SUB_STYLE)
    ev = (
        # мягкая тень под панелью
        f"Dialogue: 0,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
        f"{{\\pos({x:.0f},{y+6:.0f})\\1c&H000000&\\1a&H96&\\bord0\\blur14\\p1}}"
        f"{rrect(pw, ph, 30)}{{\\p0}}\n"
        # сама панель
        f"Dialogue: 1,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
        f"{{\\pos({x:.0f},{y:.0f})\\1c&H141110&\\1a&H4A&\\bord2"
        f"\\3c&HE8F4FF&\\3a&HB4&\\blur0.6\\p1}}{rrect(pw, ph, 30)}{{\\p0}}\n"
        f"Dialogue: 2,0:00:00.00,0:00:20.00,T,,0,0,0,,"
        f"{{\\an5\\pos({W//2},{y+ph/2:.0f})}}" + "\\N".join(lines) + "\n")
    return st, ev + sub_rows()


def variant_gold():
    """B — золотая рамка: почти прозрачная панель, тонкий золотой контур."""
    size = 58
    lines = wrap(QUESTION, 22)
    tw, lh = measure(lines, size)
    pad_x, pad_y = 46, 34
    pw, ph = tw + 2 * pad_x, lh * len(lines) + 2 * pad_y
    x, y = (W - pw) / 2, 70
    st = (f"Style: T,{FONT},{size},&H00E8F4FF,&H00000000,&H00000000,"
          "-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
          f"Style: PANEL,{FONT},60,&H00FFFFFF,&H00FFFFFF,&H00000000,"
          "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n" + SUB_STYLE)
    ev = (
        f"Dialogue: 0,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
        f"{{\\pos({x:.0f},{y+5:.0f})\\1c&H000000&\\1a&HA0&\\bord0\\blur16\\p1}}"
        f"{rrect(pw, ph, 26)}{{\\p0}}\n"
        f"Dialogue: 1,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
        f"{{\\pos({x:.0f},{y:.0f})\\1c&H0F0C08&\\1a&H64&\\bord3"
        f"\\3c&H27A2C9&\\3a&H28&\\blur0.4\\p1}}{rrect(pw, ph, 26)}{{\\p0}}\n"
        f"Dialogue: 2,0:00:00.00,0:00:20.00,T,,0,0,0,,"
        f"{{\\an5\\pos({W//2},{y+ph/2:.0f})}}" + "\\N".join(lines) + "\n")
    return st, ev + sub_rows()


def variant_soft():
    """C — мягкая подложка без рамки: размытое тёмное пятно, жёлтый текст."""
    size = 60
    lines = wrap(QUESTION, 22)
    tw, lh = measure(lines, size)
    pad_x, pad_y = 60, 44
    pw, ph = tw + 2 * pad_x, lh * len(lines) + 2 * pad_y
    x, y = (W - pw) / 2, 62
    st = (f"Style: T,{FONT},{size},&H0000D4FF,&H00000000,&H00000000,"
          "-1,0,0,0,100,100,0,0,1,3,0,5,0,0,0,1\n"
          f"Style: PANEL,{FONT},60,&H00FFFFFF,&H00FFFFFF,&H00000000,"
          "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n" + SUB_STYLE)
    ev = (
        f"Dialogue: 0,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
        f"{{\\pos({x:.0f},{y:.0f})\\1c&H0A0806&\\1a&H55&\\bord0\\blur26\\p1}}"
        f"{rrect(pw, ph, 60)}{{\\p0}}\n"
        f"Dialogue: 2,0:00:00.00,0:00:20.00,T,,0,0,0,,"
        f"{{\\an5\\pos({W//2},{y+ph/2:.0f})}}" + "\\N".join(lines) + "\n")
    return st, ev + sub_rows()


def variant_capsule():
    """D — капсулы построчно: скруглённые «таблетки» с золотой чертой сверху."""
    size = 56
    lines = wrap(QUESTION, 22)
    st = (f"Style: T,{FONT},{size},&H0000D4FF,&H00000000,&H00000000,"
          "-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
          f"Style: PANEL,{FONT},60,&H00FFFFFF,&H00FFFFFF,&H00000000,"
          "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n" + SUB_STYLE)
    ev, y = "", 74
    pad_x, pad_y = 40, 20
    f = ImageFont.truetype(FONT_FILE, size)
    for i, line in enumerate(lines):
        lw = f.getlength(line)
        pw, ph = lw + 2 * pad_x, size * 1.30 + 2 * pad_y
        x = (W - pw) / 2
        ev += (
            f"Dialogue: 0,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
            f"{{\\pos({x:.0f},{y+4:.0f})\\1c&H000000&\\1a&HA8&\\bord0\\blur12\\p1}}"
            f"{rrect(pw, ph, ph/2)}{{\\p0}}\n"
            f"Dialogue: 1,0:00:00.00,0:00:20.00,PANEL,,0,0,0,,"
            f"{{\\pos({x:.0f},{y:.0f})\\1c&H120E0A&\\1a&H3C&\\bord2"
            f"\\3c&H27A2C9&\\3a&H50&\\p1}}{rrect(pw, ph, ph/2)}{{\\p0}}\n"
            f"Dialogue: 2,0:00:00.00,0:00:20.00,T,,0,0,0,,"
            f"{{\\an5\\pos({W//2},{y+ph/2:.0f})}}{line}\n")
        y += ph + 12
    return st, ev + sub_rows()


VARIANTS = {"A_glass": variant_glass, "B_gold": variant_gold,
            "C_soft": variant_soft, "D_capsule": variant_capsule}

for name, fn in VARIANTS.items():
    styles, events = fn()
    p = OUT / f"{name}.ass"
    p.write_text(head(styles) + events, encoding="utf-8")
    dst = OUT / f"{name}.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", "862", "-i", str(SRC),
         "-vf", (f"crop=608:1080:(iw-608)/2:0,scale={W}:{H}:flags=lanczos,"
                 f"setsar=1,ass={p.as_posix()}"),
         "-frames:v", "1", str(dst)], check=True)
    print(f"{name}: {dst}")
