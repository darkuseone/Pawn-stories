"""
type.py — шрифт канала и подписи: обложки, стекло в длинном, шортсы.

Один файл Oswald Bold на все три места. Узкий ультражирный гротеск — тот
же силуэт, что на превью «STOLEN FROM THE LOUVRE», и со строчными для
вопроса в шапке шортса. Лицензия SIL OFL, лежит в репозитории, на раннере
apt не нужен.

Если файла нет (локальный прогон без git-lfs, обрезанный чекаут) —
Liberation Sans / DejaVu. Сборка не падает из-за шрифта; падает качество
подписи, и это видно в смоуке («шрифт канала не найден»).

Стекло в ffmpeg/libass — имитация Apple, не backdrop-filter: полупрозрачная
заливка + слой-свечение с \\blur + тонкий светлый кант. Настоящий blur
«сквозь буквы» libass не умеет; двухслойный ASS это даёт без Remotion.
Стекло только в ДЛИННОМ ролике. Шортс непрозрачный: белый + чёрная обводка.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
FONT_FILE = ROOT / "assets" / "fonts" / "Oswald-Bold.ttf"

# Имя семейства внутри TTF — его libass ищет по Fontname. Не «Oswald Bold»:
# в таблице имён family = Oswald, subfamily = Bold, Bold=-1 в стиле ASS.
FONT_NAME = "Oswald"

FALLBACKS = (
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
)

# Название выпуска: наплыв 1–5 с поверх уже смонтированных первых кадров,
# без новой тишины в начале (крючок озвучки не режем).
OPENING_DUR = 4.6
OPENING_FADE_IN = 0.30
OPENING_FADE_OUT = 0.55

# Кегль главы — как у названия выпуска (раньше была половина, на кадре
# мелко). Оба подбираются под ширину кадра, длинное имя главы уйдёт в
# две-три строки, а не ужмётся обратно в мелкий кегль.
OPENING_FS = 132
CHAPTER_FS = 132


def font_path() -> Path:
    """Рабочий TTF: канал, иначе первый существующий запасной."""
    if FONT_FILE.exists():
        return FONT_FILE
    for p in FALLBACKS:
        if p.exists():
            return p
    return FONT_FILE


def font_ok() -> bool:
    return FONT_FILE.exists() and FONT_FILE.stat().st_size > 1000


def font_name() -> str:
    """Fontname для libass. Запасной файл — Liberation Sans."""
    if FONT_FILE.exists():
        return FONT_NAME
    return "Liberation Sans"


def fontsdir() -> str:
    """Каталог для ass=...:fontsdir=. На раннере это assets/fonts из репо."""
    p = font_path()
    return str(p.parent if p.exists() else FONT_FILE.parent)


def font_files() -> list[str]:
    """Список путей для замера PIL: канал первым, запасные следом."""
    out = []
    if FONT_FILE.exists():
        out.append(str(FONT_FILE))
    for p in FALLBACKS:
        if p.exists() and str(p) not in out:
            out.append(str(p))
    return out


def ass_time(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _upper(s: str) -> str:
    return " ".join((s or "").split()).upper()


def _split_overlay(overlay: str) -> tuple[str, str]:
    """
    Одна строка «КРЮЧОК. ЦИФРА» — два поля обложки, не один длинный
    заголовок. На 120 px в ленте вторая фраза всё равно не влезет в kicker.
    """
    raw = (overlay or "").replace("|", ".")
    bits = [p.strip(" .") for p in re.split(r"[.;|]+", raw) if p.strip(" .")]
    if (len(bits) >= 2
            and 1 <= len(bits[0].split()) <= 6
            and 1 <= len(bits[1].split()) <= 8):
        return _upper(bits[0]), _upper(bits[1])
    if bits:
        return _upper(bits[0]), _upper(bits[1]) if len(bits) > 1 else ""
    return "", ""


def cover_lines(job: dict) -> tuple[str, str]:
    """
    Крючок обложки (3–5 слов) и короткая вторая строка с цифрой.

    Не полный youtube.title: в мобильной ленте ~120–150 px длинный заголовок
    не читается. Спецификация выигрывает (cover_kicker / cover_sub), иначе
    черновик из _превью_промпт.overlay_text (вторая фраза после точки —
    sub), иначе двоеточие в title / первые пять слов.
    """
    y = job.get("youtube") or {}
    kicker = _upper(y.get("cover_kicker") or "")
    sub = _upper(y.get("cover_sub") or "")
    if kicker:
        return kicker, sub
    overlay = ((job.get("_превью_промпт") or {}).get("overlay_text") or "").strip()
    if overlay:
        a, b = _split_overlay(overlay)
        if a:
            return a, b
    title = (y.get("title") or job.get("id") or "").strip()
    if ":" in title:
        a, b = title.split(":", 1)
        return _upper(a), _upper(b)
    words = title.split()
    return _upper(" ".join(words[:5])), _upper(" ".join(words[5:8]))


def wrap_text(text: str, per_line: int) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def measure_width(lines, size: int, files=None) -> float:
    """Ширина самой широкой строки настоящим шрифтом. Нет PIL — оценка."""
    if not lines:
        return 0.0
    files = files if files is not None else font_files()
    try:
        from PIL import ImageFont
    except ImportError:
        return max(len(l) for l in lines) * size * 0.52
    path = next((p for p in files if Path(p).exists()), None)
    if not path:
        return max(len(l) for l in lines) * size * 0.52
    f = ImageFont.truetype(path, size)
    return max(f.getlength(l) for l in lines)


def fit_size(lines, max_w: int, size: int, floor: int = 30, files=None) -> int:
    """Уменьшает кегль, пока самая длинная строка не влезет в max_w."""
    if not lines:
        return size
    files = files if files is not None else font_files()
    try:
        from PIL import ImageFont
    except ImportError:
        return size
    path = next((p for p in files if Path(p).exists()), None)
    if not path:
        return size
    while size > floor:
        f = ImageFont.truetype(path, size)
        if max(f.getlength(l) for l in lines) <= max_w:
            break
        size -= 2
    return size


def wrap_to_width(text: str, size: int, max_w: int, max_lines: int = 4) -> list[str]:
    """Перенос по словам с замером ширины, не по числу символов."""
    words = (text or "").split()
    if not words:
        return []
    files = font_files()
    lines, cur = [], ""
    for w in words:
        probe = (cur + " " + w).strip()
        if cur and measure_width([probe], size, files=files) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = probe
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        # не влезло — склеиваем хвост в последнюю разрешённую строку
        head, tail = lines[: max_lines - 1], lines[max_lines - 1 :]
        return head + [" ".join(tail)]
    return lines


def _ass_styles(w: int, h: int, fs: int) -> str:
    name = font_name()
    # Glow / Fill / Edge — три слоя одного слова. Primary белый,
    # Outline у Edge — холодный светлый кант (стекло), не чёрная обводка.
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Glow,{name},{fs},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,40,40,40,1
Style: Fill,{name},{fs},&H00FFFFFF,&H00E8F4FF,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,40,40,40,1
Style: Edge,{name},{fs},&H00FFFFFF,&H00E8F4FF,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def glass_events(lines, start: float, end: float, size: int,
                 cx: int, cy: int, fade_in: float, fade_out: float,
                 extra: str = "") -> list[str]:
    """
    Три слоя стекла: свечение (blur), кант, заливка.

    extra — дополнительные тэги (\\fscx, \\move) на все слои сразу.
    """
    if not lines:
        return []
    text = "\\N".join(ass_escape(l) for l in lines)
    fad = f"\\fad({int(fade_in * 1000)},{int(fade_out * 1000)})"
    pos = f"\\an5\\pos({int(cx)},{int(cy)})"
    tag = f"{pos}{fad}{extra}\\fs{int(size)}"
    t0, t1 = ass_time(start), ass_time(end)
    # \\1a: 00 непрозрачный, FF пустой. Glow держим прозрачнее заливки.
    return [
        f"Dialogue: 0,{t0},{t1},Glow,,0,0,0,,{{{tag}\\blur18\\1a&H70&}}{text}",
        f"Dialogue: 1,{t0},{t1},Edge,,0,0,0,,{{{tag}\\blur0.4\\bord2.4\\3c&HE8F4FF&\\3a&H50&\\1a&H28&}}{text}",
        f"Dialogue: 2,{t0},{t1},Fill,,0,0,0,,{{{tag}\\blur0.8\\1a&H18&}}{text}",
    ]


def _fit_opening_lines(kicker: str, sub: str, w: int, h: int) -> tuple[list[str], int, int]:
    """Кегль названия почти на весь кадр; подзаголовок меньше."""
    max_w = int(w * 0.90)
    fs = OPENING_FS
    lines = wrap_to_width(kicker, fs, max_w, max_lines=3) or [kicker]
    fs = fit_size(lines, max_w, fs, floor=64)
    lines = wrap_to_width(kicker, fs, max_w, max_lines=3) or lines
    sub_fs = max(36, int(fs * 0.42))
    return lines, fs, sub_fs


def write_opening_ass(job: dict, path: Path, w: int = 1920, h: int = 1080) -> Path:
    """Название выпуска, 0…OPENING_DUR, то же kicker что на обложке."""
    kicker, sub = cover_lines(job)
    lines, fs, sub_fs = _fit_opening_lines(kicker, sub, w, h)
    body = _ass_styles(w, h, fs)
    cx, cy = w // 2, int(h * 0.46)
    events = glass_events(
        lines, 0.0, OPENING_DUR, fs, cx, cy,
        OPENING_FADE_IN, OPENING_FADE_OUT)
    if sub:
        sub_lines = wrap_to_width(sub, sub_fs, int(w * 0.82), max_lines=2) or [sub]
        gap = int(fs * 0.85) * max(len(lines), 1) / 2 + int(sub_fs * 0.9)
        events += glass_events(
            sub_lines, 0.0, OPENING_DUR, sub_fs, cx, cy + int(gap),
            OPENING_FADE_IN, OPENING_FADE_OUT)
    path.write_text(body + "\n".join(events) + "\n", encoding="utf-8")
    return path


def write_chapter_ass(text: str, path: Path, dur: float,
                      w: int = 1920, h: int = 1080) -> Path:
    """Карточка главы: тот же кегль, что у названия выпуска, fade внутри паузы."""
    title = " ".join((text or "").split())
    fs = CHAPTER_FS
    max_w = int(w * 0.86)
    lines = wrap_to_width(title, fs, max_w, max_lines=3) or [title]
    fs = fit_size(lines, max_w, fs, floor=36)
    lines = wrap_to_width(title, fs, max_w, max_lines=3) or lines
    fade = min(0.32, max(0.18, dur / 8))
    body = _ass_styles(w, h, fs)
    events = glass_events(lines, 0.0, dur, fs, w // 2, h // 2, fade, fade)
    path.write_text(body + "\n".join(events) + "\n", encoding="utf-8")
    return path
