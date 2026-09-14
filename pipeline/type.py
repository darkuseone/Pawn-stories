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


# ─────────────────────── СУБТИТРЫ С НАРАСТАЮЩЕЙ ПОДСВЕТКОЙ ───────────────────────
#
# Одна реализация на длинный ролик и на шортс: шрифт, две ступени яркости
# и способ резать строку обязаны совпадать, иначе шортс перестаёт выглядеть
# куском того же канала.
#
# МЕХАНИКА — «ЗАЛИВКА», А НЕ БЕГУЩЕЕ СЛОВО. Строка выводится целиком
# приглушённой, и по мере речи каждое слово ПО ОЧЕРЕДИ выходит на полную
# яркость И ТАК И ОСТАЁТСЯ. К концу фразы подсвечена вся строка, граница
# света — это и есть место, где сейчас голос.
#
# Первая версия делала обратное: слово вспыхивало и гасло обратно, то есть
# подсвечено было ровно одно слово. Это другой приём, и он проигрывает:
# прочитанное гаснет, глаз теряет, докуда дошёл, и строку приходится
# перечитывать. Референс оформления канала (GSAP-композиция
# mk-callout-highlight: один скаляр 0..1 гонит emphasis по предложению,
# слова тускло-серые впереди и полные позади) описывает именно заливку.
#
# ПОЧЕМУ НЕ ТЕГ \k, хотя он делает ровно это. \k считает в СОТЫХ долях
# секунды и идёт подряд от начала события: сумма всех \k обязана совпасть
# с длиной события. У нас предложение режется на куски по ширине строки, и
# на стыках это давало бы накопленную ошибку округления. \t берёт
# абсолютные миллисекунды каждого слова — то, что измерено выравниванием
# ElevenLabs, — и на стыках ничего не копит. Плюс у \t есть длительность
# перехода: слово не щёлкает, а проявляется.
#
# ПОЧЕМУ ASS, А НЕ drawtext. Перекрасить ОДНО слово внутри уже показанной
# строки drawtext не умеет вовсе: пришлось бы класть отдельный слой на
# каждое слово и считать координаты руками — десятки фильтров на фразу.
# libass делает это одним событием.
#
# ПОЧЕМУ НЕ БРАУЗЕРНЫЙ РЕНДЕР. Референс написан на GSAP и рисуется в
# Chromium. Взята из него МЕХАНИКА, а не движок: на получасовом ролике это
# ~54 000 кадров через headless-браузер плюс Node и npm в репозитории, где
# сейчас только Python и ffmpeg (см. шапку shorts.py). libass даёт тот же
# кадр в том же проходе, который и так уже идёт ради названия выпуска.

# Полная яркость — не чистый белый, а #f5f5f7 из референса: на тёмном
# цветокоре канала чистый 255 «звенит» и тянет взгляд с картинки.
# Формат ASS — &HBBGGRR&, то есть байты в обратном порядке.
SUB_FULL = "&HF7F5F5&"

# Приглушённая ступень — ПРОЗРАЧНОСТЬЮ, а не серым цветом: серый работает
# только на известном фоне, а прозрачность одинаково тускла и на тёмном
# кадре, и на светлом. В ASS 00 — непрозрачно, FF — пусто; &HA0& это ~37%,
# рядом с 0.32 из референса.
SUB_DIM_A = "&HA0&"

# Обводка НЕ гаснет вместе с заливкой. В референсе фон известен и чистый, у
# нас под субтитром движущийся кадр: приглушённое слово без обводки на
# светлом куске исчезает совсем. Чёрный кант держит читаемость на обеих
# ступенях, и это сознательное отступление от референса.

# Сколько длится проявление одного слова. Мгновенная перекраска на быстрой
# речи читается как мигание, полсекунды — как запаздывание.
SWEEP_MS = 200

# Вход строки: короткое всплытие и проявление, как animationIn в референсе
# (там 24 px за 0.7 с на весь блок). У нас событие живёт 2-4 секунды, и на
# каждом куске такой вход был бы качкой — поэтому он ставится ТОЛЬКО на
# первое событие фразы, после паузы в речи (см. ENTER_GAP).
ENTER_MS = 320
ENTER_RISE = 18
ENTER_GAP = 0.45

# Субтитры длинного ролика. Низ кадра — их и только их: карточка на числе
# (editorial/textcard.py) с этой правки стоит в ВЕРХНЕЙ трети именно
# потому, что здесь живёт субтитр. Полоса примерно 0.81-0.93 высоты.
SUB_LONG_Y = 0.87
SUB_LONG_FS = 58
SUB_LONG_MAX_LINES = 2

# Ширина колонки субтитра — доля кадра. Текст внутри неё выключен ВЛЕВО,
# как в референсе (maxWidth + textAlign: left): строка с ровным левым краем
# читается быстрее центрованной, потому что глаз всегда знает, где начало.
SUB_LONG_COL = 0.66


def _word_starts(words, start: float):
    """Начало каждого слова в миллисекундах ОТ НАЧАЛА события."""
    return [(w["w"], int(max(0.0, float(w["s"]) - start) * 1000)) for w in words]


def karaoke_line(words, start: float, end: float, per_line_px: int,
                 size: int, max_lines: int = 2) -> str:
    """
    Строка, по которой подсветка идёт ЗАЛИВКОЙ: слово выходит на полную
    яркость к своему тайм-коду и таким и остаётся до конца события.

    Аргумент end оставлен в сигнатуре: он задаёт конец события и нужен
    вызывающему, а самой разметке — нет, у каждого слова своё абсолютное
    время.

    Перенос считается ЗАМЕРОМ шрифта (measure_width), а не числом символов:
    «illinois» и «MMMMMMMM» одной длины занимают втрое разную ширину, а
    одно длинное слово переносом не режется вовсе — на этом уже один раз
    выехали буквы за края кадра (см. fit_size).
    """
    tags = _word_starts(words, start)
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

    def one(w, a):
        # Первое слово куска проявляется вместе с самим событием, поэтому
        # его переход начинается с нуля, а не с отрицательного времени.
        a = max(0, a)
        return (f"{{\\1a{SUB_DIM_A}\\t({a},{a + SWEEP_MS},\\1a&H00&)}}"
                + ass_escape(w))

    return "\\N".join(" ".join(one(*t) for t in line) for line in lines)


def sub_tag(x: int, y: int, an: int = 4, enter: bool = False) -> str:
    """
    Позиция события и, если это начало фразы, вход: всплытие + проявление.

    \\move заменяет \\pos, поэтому оба сразу не ставятся. \\fad даёт
    проявление; гашение на выходе не нужно — следующий кусок фразы
    начинается ровно там, где кончился предыдущий, и мигание между ними
    читалось бы как брак.
    """
    if not enter:
        return f"{{\\an{an}\\pos({x},{y})}}"
    return (f"{{\\an{an}\\move({x},{y + ENTER_RISE},{x},{y},0,{ENTER_MS})"
            f"\\fad({ENTER_MS},0)}}")


def plain_chunks(text: str, per_line_px: int, size: int, max_lines: int = 2):
    """
    Предложение без измеренных слов — на куски по max_lines строк.

    Запасной путь: субтитры в ролике должны быть ВСЕГДА, а поддельные
    тайм-коды слов — нет. Границы самого предложения измерены и без поля
    words, поэтому кусок стоит на своём месте, просто не заливается.

    Резать всё равно приходится: длинное предложение в две строки не
    влезает, а wrap_to_width склеивает хвост в последнюю разрешённую
    строку — то есть теряет слова за краем кадра. Внутри предложения куски
    делятся ПРОПОРЦИОНАЛЬНО числу символов: это оценка, и на подсветку её
    пускать нельзя, но на смену строки её точности хватает — так же
    устроены субтитры шортса (shorts.lines_with_times).
    """
    words = (text or "").split()
    if not words:
        return []
    files = font_files()
    out, cur = [], []
    for w in words:
        probe = " ".join(cur + [w])
        need = measure_width([probe], size, files=files) / max(per_line_px, 1)
        if cur and need > max_lines:
            out.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        out.append(" ".join(cur))
    return out


def plain_line(text: str, per_line_px: int, size: int,
               max_lines: int = 2) -> str:
    """Один кусок без заливки, с переносом по замеру шрифта."""
    lines = wrap_to_width(text, size, per_line_px, max_lines=max_lines)
    if not lines:
        return ""
    return "\\N".join(ass_escape(l) for l in lines)


def sub_events(marks, t0: float, t1: float, size: int, per_line_px: int,
               style: str = "KSUB", max_lines: int = 2, shift: float = 0.0):
    """
    События субтитров на отрезке [t0, t1]. Список кортежей
    (начало, конец, стиль, текст события, нужен ли вход).

    Предложение длиннее max_lines строк режется на несколько событий ПО
    СЛОВАМ, а не по символам: у каждого слова свой измеренный тайм-код, и
    кусок начинается ровно тогда, когда звучит его первое слово.

    Предложение БЕЗ поля words (старый кэш) не пропускается, а выводится
    без заливки — по измеренным границам самого предложения. Субтитр
    должен быть всегда; выдумывать время слов ради подсветки — нет.
    """
    files = font_files()
    rows = []
    prev_end = None
    for m in marks:
        if m.get("end", 0) <= t0 or m.get("start", 0) >= t1:
            continue
        words = m.get("words") or []
        if not words:
            m_s, m_e = float(m.get("start", 0)), float(m.get("end", 0))
            parts = plain_chunks(m.get("text", ""), per_line_px, size,
                                 max_lines)
            span = max(m_e - m_s, 0.05)
            total_chars = sum(len(c) for c in parts) or 1
            cur_t = m_s
            for c in parts:
                dur = span * len(c) / total_chars
                s_, e_ = cur_t, cur_t + dur
                cur_t = e_
                body = plain_line(c, per_line_px, size, max_lines)
                if not body or e_ <= s_:
                    continue
                enter = prev_end is None or (s_ - prev_end) > ENTER_GAP
                rows.append((s_ - t0 + shift, e_ - t0 + shift, style, body,
                             enter))
                prev_end = e_
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
            # Вход ставится только там, где перед куском была пауза в речи:
            # внутри фразы куски идут встык, и всплытие на каждом читалось
            # бы как качка строки.
            enter = prev_end is None or (s - prev_end) > ENTER_GAP
            rows.append((s - t0 + shift, e - t0 + shift, style, body, enter))
            prev_end = e
    return rows


# ─────────────────────── СЛОЙ НАДПИСЕЙ ДЛИННОГО РОЛИКА ───────────────────────
#
# ОДИН ФАЙЛ ASS НА ВСЁ, И ОДИН ПРОХОД FFMPEG.
#
# Второй проход по готовому silent.mp4 уже был — им жглось название
# выпуска (build.py, render.burn_ass). Проход перекодирует ролик целиком,
# то есть 70-90 минут ffmpeg на получасовом ролике уже оплачены. Всё
# остальное, что рисуется буквами — субтитры с заливкой по словам и
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
    # картинку. Цвет заливки — SUB_FULL (#f5f5f7), приглушённую ступень
    # даёт \1a в самом событии. Выключка ВЛЕВО: Alignment 4 — середина по
    # вертикали, левый край по горизонтали, а точку ставит \pos.
    # Полей ровно столько, сколько в строке Format выше (там НЕТ
    # SecondaryColour). Лишнее поле сдвигает весь ряд, и libass читает
    # кегль как цвет — молча, без единой строки в лог.
    fill = "&H00" + SUB_FULL.strip("&H&")
    sub = (f"Style: KSUB,{name},{sub_fs},{fill},&H00000000,&H00000000,"
           f"-1,0,0,0,100,100,0,0,1,4,1,4,60,60,60,1\n")
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
    Предложение без поля words выводится без заливки, по измеренным
    границам самого предложения: субтитр в ролике есть ВСЕГДА, выдуманных
    тайм-кодов слов — нет.
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
        col = int(w * SUB_LONG_COL)
        rows = sub_events(marks, 0.0, float(total or 10 ** 9), sub_fs,
                          col, style="KSUB",
                          max_lines=SUB_LONG_MAX_LINES)
        y_px = int(h * SUB_LONG_Y)
        # Колонка выключена влево, но сама стоит по центру кадра: ровный
        # левый край даёт глазу постоянную точку начала строки, а
        # центрованная колонка не сдвигает композицию вбок.
        x_px = (w - col) // 2
        for s, e, style, text, enter in rows:
            if kicker and e <= OPENING_DUR:
                continue
            s = max(s, OPENING_DUR if kicker else 0.0)
            if e - s < 0.08:
                continue
            events.append(
                f"Dialogue: 3,{ass_time(s)},{ass_time(e)},{style},,0,0,0,,"
                + sub_tag(x_px, y_px, an=4, enter=enter) + text)

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
