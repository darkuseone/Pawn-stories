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


# Загруженные шрифты по (файл, кегль). ImageFont.truetype читает TTF с
# диска каждым вызовом, а замер теперь идёт НА КАЖДОЕ СЛОВО субтитра: на
# получасовом ролике это десятки тысяч открытий одного и того же файла.
# Кегля в ролике два-три, так что словарь остаётся крошечным.
_FONT_CACHE = {}


def _loaded(path: str, size: int):
    key = (path, int(size))
    if key not in _FONT_CACHE:
        from PIL import ImageFont
        _FONT_CACHE[key] = ImageFont.truetype(path, int(size))
    return _FONT_CACHE[key]


def measure_width(lines, size: int, files=None) -> float:
    """Ширина самой широкой строки настоящим шрифтом. Нет PIL — оценка."""
    if not lines:
        return 0.0
    files = files if files is not None else font_files()
    try:
        import PIL.ImageFont  # noqa: F401
    except ImportError:
        return max(len(l) for l in lines) * size * 0.52
    path = next((p for p in files if Path(p).exists()), None)
    if not path:
        return max(len(l) for l in lines) * size * 0.52
    f = _loaded(path, size)
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
        f = _loaded(path, size)
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


# ─────────────────────── КАРАОКЕ-СУБТИТРЫ ───────────────────────
#
# Одна реализация на длинный ролик и на шортс: шрифт, цвет подсветки и
# способ резать строку обязаны совпадать, иначе шортс перестаёт выглядеть
# куском того же канала.
#
# ПОЧЕМУ ASS, А НЕ drawtext. Перекрасить ОДНО слово внутри уже показанной
# строки drawtext не умеет вовсе: пришлось бы класть отдельный слой на
# каждое слово и считать координаты руками — десятки фильтров на фразу.
# libass делает это одним событием.
#
# ПОЧЕМУ НЕ ТЕГ \k. Караоке-тег \k красит слово и ОСТАВЛЯЕТ его крашеным
# до конца строки: к концу фразы подсвечено всё, и подсветка перестаёт
# показывать, где голос. Нужен противоположный эффект — бегущее слово.
# Он делается парой \t на каждом слове, см. karaoke_line().
#
# ЦВЕТ. Тёплый янтарь (AMBER) — тема канала «деньги/находка», и он же
# отличает подсветку от белого текста при любой яркости фона. Резкий
# лимонно-жёлтый на светлом кадре сливается, поэтому взят приглушённый.
# Тот же цвет у плашек на числах (editorial/textcard.AMBER_RGB): плашка и
# подсвеченное слово — один приём канала.

# Тёплый янтарь в формате ASS (&HBBGGRR&): R=255 G=196 B=64.
AMBER = "&H40C4FF&"

# Субтитры длинного ролика. Низ кадра занят плашками на числах
# (editorial/textcard.py, PLACES: полоса 0.760-0.816 высоты), поэтому
# строка ставится ВЫШЕ них, а не под ними.
SUB_LONG_Y = 0.87
SUB_LONG_FS = 58
SUB_LONG_MAX_LINES = 2


def _kara_tags(words, start: float, end: float):
    """
    Окно подсветки каждого слова в миллисекундах ОТ НАЧАЛА события.

    Конец слова тянется до начала следующего: пауза между словами
    отдаётся предыдущему, иначе подсветка мигает и «догоняет» голос.
    """
    if not words:
        return []
    out, cur = [], float(start)
    for i, w in enumerate(words):
        nxt = float(end) if i == len(words) - 1 else float(words[i + 1]["s"])
        out.append((w["w"], int(max(0.0, cur - start) * 1000),
                    int(max(0.0, nxt - start) * 1000)))
        cur = nxt
    return out


# Мягкий переход подсветки. Мгновенная перекраска на быстрой речи читается
# как мигание, четверть секунды — как запаздывание. 90/140 мс — вход чуть
# резче выхода, так слово «вспыхивает» и спокойно отпускает.
KARA_IN_MS = 90
KARA_OUT_MS = 140


def karaoke_line(words, start: float, end: float, per_line_px: int,
                 size: int, max_lines: int = 2,
                 base: str = "&HFFFFFF&", hot: str = AMBER) -> str:
    """
    Строка субтитра, где ПОДСВЕЧЕНО ТОЛЬКО ЗВУЧАЩЕЕ СЕЙЧАС слово.

    Почему не голый караоке-тег
    ---------------------------
    Тег \\k красит слово и ОСТАВЛЯЕТ его крашеным до конца строки: к концу
    фразы подсвечено всё, и подсветка перестаёт показывать, где голос.
    Нужен ровно противоположный эффект — бегущее слово. Он делается парой
    \\t на каждом слове: цвет уходит в янтарь к началу слова и возвращается
    в белый к его концу. Тег действует на текст ПОСЛЕ себя и до следующего
    блока, а блок стоит у каждого слова — поэтому слова не мешают друг другу.

    Перенос считается ЗАМЕРОМ шрифта (measure_width), а не числом символов:
    «illinois» и «MMMMMMMM» одной длины занимают втрое разную ширину, а
    одно длинное слово переносом не режется вовсе — на этом уже один раз
    выехали буквы за края кадра (см. fit_size).
    """
    tags = _kara_tags(words, start, end)
    if not tags:
        return ""
    files = font_files()
    lines, cur = [], []
    for item in tags:
        probe = " ".join([t[0] for t in cur] + [item[0]])
        if cur and measure_width([probe], size, files=files) > per_line_px:
            lines.append(cur)
            cur = [item]
        else:
            cur.append(item)
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        head, tail = lines[:max_lines - 1], lines[max_lines - 1:]
        lines = head + [[t for chunk in tail for t in chunk]]

    def one(w, a, b):
        # Короткое слово («a», «it») живёт меньше, чем длится сам переход.
        # Без этого зажима конец анимации оказывается РАНЬШЕ её начала —
        # \\t(661,653,...) libass молча не рисует, и слово не подсвечивается
        # вовсе. Окно растягивается до минимально осмысленного.
        hi = max(b, a + KARA_IN_MS + 20)
        lo = max(hi - KARA_OUT_MS, a + KARA_IN_MS)
        return (f"{{\\1c{base}"
                f"\\t({a},{a + KARA_IN_MS},\\1c{hot})"
                f"\\t({lo},{hi},\\1c{base})}}"
                + ass_escape(w))

    return "\\N".join(" ".join(one(*t) for t in line) for line in lines)


def sub_events(marks, t0: float, t1: float, size: int, per_line_px: int,
               style: str = "KSUB", max_lines: int = 2, shift: float = 0.0):
    """
    События субтитров на отрезке [t0, t1] по измеренным словам.

    Предложение длиннее max_lines строк режется на несколько событий ПО
    СЛОВАМ, а не по символам: у каждого слова свой измеренный тайм-код, и
    кусок начинается ровно тогда, когда звучит его первое слово.

    marks без поля words (старый кэш) пропускаются — лучше кадр без
    подписи, чем подпись, разъехавшаяся со звуком.
    """
    files = font_files()
    rows = []
    for m in marks:
        if m.get("end", 0) <= t0 or m.get("start", 0) >= t1:
            continue
        words = m.get("words") or []
        if not words:
            continue
        chunk, out_chunks = [], []
        for w in words:
            probe = chunk + [w]
            text = " ".join(x["w"] for x in probe)
            need = measure_width([text], size, files=files) / max(per_line_px, 1)
            if chunk and need > max_lines:
                out_chunks.append(chunk)
                chunk = [w]
            else:
                chunk = probe
        if chunk:
            out_chunks.append(chunk)
        for k, ch in enumerate(out_chunks):
            s = float(ch[0]["s"])
            e = (float(out_chunks[k + 1][0]["s"])
                 if k + 1 < len(out_chunks) else float(m["end"]))
            if e <= s:
                continue
            body = karaoke_line(ch, s, e, per_line_px, size,
                                max_lines=max_lines)
            if not body:
                continue
            rows.append((s - t0 + shift, e - t0 + shift, style, body))
    return rows


# ─────────────────────── СЛОЙ НАДПИСЕЙ ДЛИННОГО РОЛИКА ───────────────────────
#
# ОДИН ФАЙЛ ASS НА ВСЁ, И ОДИН ПРОХОД FFMPEG.
#
# Второй проход по готовому silent.mp4 уже был — им жглось название
# выпуска (build.py, render.burn_ass). Проход перекодирует ролик целиком,
# то есть 70-90 минут ffmpeg на получасовом ролике уже оплачены. Всё
# остальное, что рисуется буквами — субтитры с бегущей подсветкой и
# карточка-итог в конце, — стоит в этом проходе РОВНО НОЛЬ: libass рисует
# их тем же фильтром, что и стекло названия.
#
# Именно поэтому субтитры сюда можно было вернуть, не заплатив ничем.
# Отдельный проход ради них стоил бы второго полного перекодирования.

OUTRO_DUR = 5.0            # сколько висит карточка-итог
OUTRO_TAIL = 0.4           # и за сколько до самого конца она уходит
OUTRO_FS = 104


def _overlay_styles(w: int, h: int, fs: int, sub_fs: int) -> str:
    """Стили стекла (название, итог) плюс непрозрачный стиль субтитра."""
    body = _ass_styles(w, h, fs)
    name = font_name()
    # Субтитр в длинном ролике — НЕ стекло: стекло держит одну короткую
    # надпись в центре кадра, а строка, живущая весь ролик, на нём мылит
    # картинку. Белый с чёрной обводкой, как в шортсе, — одна подача.
    # Полей ровно столько, сколько в строке Format выше (там НЕТ
    # SecondaryColour). Лишнее поле сдвигает весь ряд, и libass читает
    # кегль как цвет — молча, без единой строки в лог.
    sub = (f"Style: KSUB,{name},{sub_fs},&H00FFFFFF,&H00000000,&H00000000,"
           f"-1,0,0,0,100,100,0,0,1,4,1,5,60,60,60,1\n")
    return body.replace("\n[Events]", "\n" + sub + "\n[Events]")


def _has_number(s: str) -> bool:
    """Есть ли в строке цифра или знак валюты."""
    return any(ch.isdigit() for ch in (s or "")) or any(
        ch in (s or "") for ch in "$£€")


def outro_lines(job: dict) -> tuple[str, str]:
    """
    Что написать в конце: итоговая сумма и призыв.

    Сумма — то же поле, что и на обложке (cover_sub): цифра, ради которой
    ролик открыли, последний раз показывается цифрами. Призыв общий на
    канал (channel/defaults.json, outro_cta) — в семи спецификациях ему
    делать нечего.

    Концовка НЕ «затемнение, как будто зритель уснул»: этот приём из
    сонного документального формата, а здесь история про азарт и находку.
    Надпись ложится поверх последних кадров, картинка не гаснет.
    """
    y = job.get("youtube") or {}
    _kicker, sub = cover_lines(job)
    top = _upper(y.get("outro_recap") or "")
    if not top and sub and _has_number(sub):
        # cover_sub берётся ТОЛЬКО если в нём действительно есть число.
        # Когда полей cover_kicker/cover_sub в спецификации нет, они
        # собираются из заголовка механически — у pawn-01 так выходит
        # «THE BACK FIRST», и в концовке это читается как обрывок фразы,
        # а не как итог. Рекап без цифры не рекап.
        top = sub
    cta = _upper(y.get("outro_cta") or job.get("outro_cta") or "")
    return top, cta


def write_overlay_ass(job: dict, path: Path, marks=None, total: float = 0.0,
                      w: int = 1920, h: int = 1080,
                      subs: bool = True) -> Path:
    """
    Название выпуска + субтитры с подсветкой + карточка-итог, одним файлом.

    marks — тайм-коды на шкале ГОТОВОГО ролика (со сдвигом на паузы глав).
    Без поля words у предложения субтитр не пишется вовсе: подпись, гуляющая
    относительно голоса, хуже отсутствующей.
    """
    kicker, sub = cover_lines(job)
    lines, fs, sub_fs_title = _fit_opening_lines(kicker, sub, w, h)
    sub_fs = SUB_LONG_FS
    body = _overlay_styles(w, h, fs, sub_fs)
    events = []

    if kicker:
        cx, cy = w // 2, int(h * 0.46)
        events += glass_events(lines, 0.0, OPENING_DUR, fs, cx, cy,
                               OPENING_FADE_IN, OPENING_FADE_OUT)
        if sub:
            sub_lines = wrap_to_width(sub, sub_fs_title, int(w * 0.82),
                                      max_lines=2) or [sub]
            gap = int(fs * 0.85) * max(len(lines), 1) / 2 + int(sub_fs_title * 0.9)
            events += glass_events(sub_lines, 0.0, OPENING_DUR, sub_fs_title,
                                   cx, cy + int(gap),
                                   OPENING_FADE_IN, OPENING_FADE_OUT)

    if subs and marks:
        # Название выпуска и субтитр не должны висеть одновременно: первые
        # секунды принадлежат крючку, а не подписи.
        # Ширина строки — две трети кадра, а не вся доступная. На всю
        # ширину в строку влезает четырнадцать слов, и глаз ведёт её
        # дольше, чем звучит фраза: подпись начинает мешать картинке,
        # ради которой ролик и смотрят.
        rows = sub_events(marks, 0.0, float(total or 10 ** 9), sub_fs,
                          int(w * 0.66), style="KSUB",
                          max_lines=SUB_LONG_MAX_LINES)
        y_px = int(h * SUB_LONG_Y)
        for s, e, style, text in rows:
            if kicker and e <= OPENING_DUR:
                continue
            s = max(s, OPENING_DUR if kicker else 0.0)
            if e - s < 0.08:
                continue
            events.append(
                f"Dialogue: 3,{ass_time(s)},{ass_time(e)},{style},,0,0,0,,"
                f"{{\\an5\\pos({w // 2},{y_px})}}{text}")

    top, cta = outro_lines(job)
    if total and (top or cta):
        end = max(0.0, float(total) - OUTRO_TAIL)
        start = max(0.0, end - OUTRO_DUR)
        cy = int(h * 0.44)
        if top:
            t_lines = wrap_to_width(top, OUTRO_FS, int(w * 0.86),
                                    max_lines=2) or [top]
            t_fs = fit_size(t_lines, int(w * 0.86), OUTRO_FS, floor=54)
            events += glass_events(t_lines, start, end, t_fs, w // 2, cy,
                                   0.45, 0.6)
        if cta:
            c_fs = max(38, int(OUTRO_FS * 0.42))
            c_lines = wrap_to_width(cta, c_fs, int(w * 0.8), max_lines=2) or [cta]
            events += glass_events(c_lines, start, end, c_fs, w // 2,
                                   cy + int(OUTRO_FS * 1.05), 0.45, 0.6)

    path.write_text(body + "\n".join(events) + "\n", encoding="utf-8")
    return path
