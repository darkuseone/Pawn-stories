"""
youtube.py — готовит всё, что нужно для загрузки готового ролика.

Запускается ПОСЛЕ монтажа:  python pipeline/youtube.py jobs/lhc-01.json

Что делает:
  1. Считает тайм-коды глав. Не выдумывает их: находит в субтитрах первое
     предложение каждого блока сценария и берёт его время. Значит главы
     всегда совпадают с тем, что реально звучит в ролике.
  2. Собирает описание — вступление, главы, примечание, теги.
  3. Режет превью из самого ролика и кладёт на него заголовок.

Редакторская часть (заголовок, названия глав, теги) живёт в блоке youtube
внутри спецификации ролика. Здесь только механика. Так текст правится там
же, где всё остальное про ролик, и не растворяется в коде.

Главы по правилам YouTube: первая обязательно с 00:00, минимум три штуки,
каждая не короче десяти секунд. Всё это проверяется, а не подразумевается.
"""

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job
import type as type_mod
import shorts as shorts_mod

W_THUMB, H_THUMB = 1280, 720
FONT_BOLD = str(type_mod.font_path())
FONT_PLAIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TAGS_LIMIT = 500          # столько символов YouTube пускает в поле тегов


def log(*a):
    print(*a, flush=True)


def norm(s: str) -> str:
    """
    Схлопывает текст до букв и цифр — для сравнения субтитра со сценарием.

    Раньше здесь стояло re.sub(r"[^a-z0-9 ]", "", s), то есть выбрасывалось
    ВСЁ, кроме латиницы. На русском сценарии от блока не оставалось ничего,
    кроме пробелов, начало главы не находилось никогда, и youtube.py падал с
    «не нашёл её начало в субтитрах» на любом ролике. Ловилось только на
    последнем шаге, после всего рендера.

    Теперь выбрасывается пунктуация, а буквы любого алфавита остаются.
    Заодно схлопываются пробелы: в сценарии между предложениями бывает два
    пробела или перенос строки, а в субтитрах — один, и подстрока не
    находилась из-за этого тоже.
    """
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def read_srt(path: Path):
    """[(секунда, нормализованный текст), ...] по порядку."""
    cues = []
    for chunk in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = chunk.strip().split("\n")
        if len(lines) < 3:
            continue
        m = re.match(r"(\d\d):(\d\d):(\d\d),(\d\d\d) -->", lines[1])
        if not m:
            continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        cues.append((t, norm(" ".join(lines[2:]))))
    return cues


def as_list(value, field="tags", sep=","):
    """
    Приводит поле спецификации к списку. Строку разбирает по разделителю.

    Спецификации роликов пишутся в чате и приезжают JSON-ом, и строка
    вместо списка — опечатка, которую глазами не видно: в файле лежит
    "тег один, тег два", выглядит совершенно нормально.

    А дальше `", ".join(строка)` перебирает её ПОСИМВОЛЬНО и склеивает
    буквы через запятую. На ff-ep05 это дало 341 «тег» по одной букве и
    строку в 1021 символ при лимите в 500 — то есть падение на самом
    последнем шаге, уже ПОСЛЕ полного монтажа. Дороже места для отказа в
    этом конвейере нет.

    Поэтому не роняем, а чиним и предупреждаем: намерение автора здесь
    однозначно, разделитель тот же самый, и терять из-за него сорок минут
    рендера незачем.
    """
    if not value:
        return []
    if isinstance(value, str):
        out = [t.strip() for t in value.split(sep) if t.strip()]
        log(f"  ! {field} записаны строкой, а не списком — разобрал на "
            f"{len(out)} шт. Поправь в спецификации: \"{field}\": [...]")
        return out
    return [str(t).strip() for t in value if str(t).strip()]


def stamp(sec: float) -> str:
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapters(job, cues):
    """
    Сопоставляет блоки сценария с субтитрами и возвращает [(секунда, имя)].

    Ищем по первому предложению блока. Поиск обязан идти ТОЛЬКО вперёд от
    предыдущей найденной главы: split(".")[0] режет по любой точке, включая
    точку в сокращении вроде "Jr.", и тогда ключ ("Don Lutes Jr") — не
    первое предложение, а обрывок имени, повторяющегося по всему сценарию.
    Поиск с начала списка реплик находил самое первое упоминание где угодно
    раньше по ролику, а не начало текущего блока.
    """
    names = job["youtube"]["chapters"]
    blocks = job["script_blocks"]
    if len(names) != len(blocks):
        raise SystemExit(f"глав {len(names)}, а блоков сценария {len(blocks)} — "
                         "их должно быть поровну")

    out = []
    search_from = 0
    for i, (block, name) in enumerate(zip(blocks, names)):
        key = norm(block.strip().split(".")[0])[:45]
        hit = None
        for idx in range(search_from, len(cues)):
            t, txt = cues[idx]
            if key and key in txt:
                hit = t
                search_from = idx + 1
                break
        if hit is None:
            raise SystemExit(f"глава {i+1} «{name}»: не нашёл её начало в субтитрах")
        out.append((hit, name))

    out[0] = (0.0, out[0][1])          # YouTube требует, чтобы первая шла с нуля
    for (a, _), (b, nm) in zip(out, out[1:]):
        if b - a < 10:
            raise SystemExit(f"глава «{nm}» короче десяти секунд — YouTube их не покажет")
    if len(out) < 3:
        raise SystemExit("глав меньше трёх — YouTube их не покажет")
    return out


# Подписи в описании. Канал англоязычный, поэтому и умолчания английские;
# ролик на другом языке переопределяет их полем description_labels в
# спецификации, не трогая код.
LABELS = {"chapters": "Chapters", "runtime": "Runtime"}


def description(job, chaps, total):
    y = job["youtube"]
    lab = {**LABELS, **(y.get("description_labels") or {})}
    parts = [y["description_intro"].strip(), "", lab["chapters"], ""]
    parts += [f"{stamp(t)}  {name}" for t, name in chaps]
    if y.get("description_notes"):
        parts += ["", y["description_notes"].strip()]
    parts += ["", f"{lab['runtime']}: {stamp(total)}"]
    # Хештеги в описании — ТЕ ЖЕ ПЯТЬ, что в отдельном разделе комплекта.
    # Раньше описание печатало сырое youtube.hashtags (три штуки), а
    # раздел — добранные до пяти: человек копировал два разных набора из
    # одного файла и справедливо не понимал, какой из них верный.
    hashtags = hashtags_five(job, load_publish_rules())
    if hashtags:
        parts += ["", " ".join(hashtags)]
    return "\n".join(parts)


# ─────────────────────── КОМПЛЕКТ ДЛЯ ВЫКЛАДКИ ───────────────────────
#
# youtube.txt — это ВСЁ, что человек копирует в формы YouTube при выкладке:
# заголовок, описание с тайм-кодами, источники, пять хештегов, теги,
# названия двух шортсов, промпт обложки, первый комментарий, запись для
# сообщества и два промпта картинок к ней. Раньше здесь были только первые
# четыре пункта, остальное человек сочинял руками на каждый ролик.
#
# НИ ОДНОГО ЗАПРОСА К МОДЕЛИ. Упаковка пересобирается на каждой правке
# обложки (stage: post), а их у ролика пять-десять: платный текст здесь
# означал бы плату за каждую такую пересборку. Всё подставляется из
# спецификации по шаблонам из channel/publish.json — по той же схеме, по
# какой covers.py собирает промпты из channel/covers.json.
#
# Спецификация выигрывает: youtube.first_comment, youtube.community_post,
# youtube.community_prompts, youtube.short_titles, youtube.sources
# перекрывают шаблон целиком.

PUBLISH_RULES = Path(__file__).parent.parent / "channel" / "publish.json"


def load_publish_rules() -> dict:
    """Шаблоны комплекта. Ключи с подчёркиванием — комментарии, не данные."""
    if not PUBLISH_RULES.exists():
        return {}
    try:
        data = json.loads(PUBLISH_RULES.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _fill(template: str, job, extra=None) -> str:
    """Плейсхолдеры шаблона. Неизвестный ключ остаётся как есть."""
    import covers
    y = job.get("youtube") or {}
    kicker, sub = type_mod.cover_lines(job)
    item, _scale = covers.visual_hooks(job)
    vals = {
        "{TITLE}": (y.get("title") or "").strip(),
        "{QUESTION}": ((job.get("open_loop") or {}).get("question") or "").strip(),
        "{SUM}": sub or kicker,
        "{ITEM}": item,
        "{ERA}": covers._era_of(job),
        "{CHAPTER}": (y.get("chapters") or [""])[0],
        "{LINK}": "<ссылка на ролик>",
    }
    vals.update(extra or {})
    out = template or ""
    for k, v in vals.items():
        out = out.replace(k, str(v))
    return out.strip()


def hashtags_five(job, rules) -> list:
    """
    Ровно пять хештегов: YouTube показывает над заголовком первые три,
    остальные работают в поиске. Не хватает своих — добираются из тегов.
    """
    want = int(rules.get("hashtag_count", 5))
    y = job.get("youtube") or {}
    out = []
    for h in as_list(y.get("hashtags"), "hashtags"):
        h = h.strip()
        if not h:
            continue
        h = h if h.startswith("#") else "#" + h
        if h.lower() not in [x.lower() for x in out]:
            out.append(h)
    for t in as_list(y.get("tags"), "tags"):
        if len(out) >= want:
            break
        tag = "#" + "".join(w.capitalize() for w in re.split(r"[^\w]+", t) if w)
        if len(tag) > 1 and tag.lower() not in [x.lower() for x in out]:
            out.append(tag)
    return out[:want]


def sources_block(job, work: Path, rules) -> str:
    """
    Источники материала — РЕАЛЬНО использованные, а не список из головы.

    Сначала манифесты скачивания (там у каждого файла записан источник),
    если их нет — объявленные в спецификации photo_sources / video_sources.
    Ссылки на исследования сюда не подставляются: в спецификации их нет, а
    подпись «по материалам X» без самого X хуже отсутствующей. Для них
    есть необязательное поле youtube.sources — что в нём лежит, то и
    печатается первым.
    """
    y = job.get("youtube") or {}
    names = rules.get("sources") or {}
    lines = []
    own = as_list(y.get("sources"), "sources") if y.get("sources") else []
    lines += [f"- {s}" for s in own if str(s).strip()]

    used = []
    for folder in ("footage", "archive"):
        man = work / "assets" / folder / "_manifest.json"
        if not man.exists():
            continue
        try:
            rows = json.loads(man.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for row in rows:
            src = str(row.get("src") or "").strip()
            if src and src not in used:
                used.append(src)
    if not used:
        used = [str(s) for s in (job.get("photo_sources") or [])]
        used += [str(s) for s in (job.get("video_sources") or []) if s not in used]
    for src in used:
        label = names.get(src) or names.get(src.replace(".", "_")) or src
        if f"- {label}" not in lines:
            lines.append(f"- {label}")
    if (job.get("image_prompts") or job.get("fill_prompts")):
        ai = names.get("xai", "AI-generated imagery")
        if f"- {ai}" not in lines:
            lines.append(f"- {ai}")
    note = (rules.get("sources_note") or "").strip()
    if note:
        lines += ["", note]
    return "\n".join(lines)


def short_titles(job, out: Path, rules) -> list:
    """
    Названия шортсов — столько, сколько их реально нарезано.

    Спецификация выигрывает (youtube.short_titles). Иначе берётся вопрос
    ТОГО блока, из которого шортс реально нарезан: shorts.py пишет это в
    out/shorts.json. Нет файла (шортсы ещё не резались или упали) —
    остаются вопросы из open_loop.questions по порядку, потом общий.
    """
    y = job.get("youtube") or {}
    own = as_list(y.get("short_titles"), "short_titles") if y.get("short_titles") else []
    want = shorts_mod.SHORT_COUNT
    if len(own) >= want:
        return [str(t).strip() for t in own[:want]]

    loop = job.get("open_loop") or {}
    per_block = {str(k): str(v).strip()
                 for k, v in (loop.get("questions") or {}).items() if str(v).strip()}
    default_q = (loop.get("question") or "").strip()

    questions = []
    cut = out / "shorts.json"
    if cut.exists():
        try:
            for row in json.loads(cut.read_text(encoding="utf-8")):
                q = str(row.get("question") or "").strip()
                questions.append(q or default_q)
        except (ValueError, OSError):
            questions = []
    if not questions:
        questions = [per_block[k] for k in sorted(per_block, key=lambda x: int(x))]
    questions = [q for q in questions if q] or ([default_q] if default_q else [])

    tpl = rules.get("short_title") or "{QUESTION} #Shorts"
    alt = rules.get("short_title_alt") or "{SUM} #Shorts"
    titles = [_fill(tpl, job, {"{QUESTION}": q}) for q in questions[:want]]
    # Добивать НАДО, и запасным шаблоном: без названия шортс уедет к
    # человеку безымянным, а он их выкладывает руками по одному.
    while len(titles) < want:
        titles.append(_fill(alt, job))
    # YouTube режет заголовок на 100 символах.
    return [t if len(t) <= 100 else t[:97].rstrip() + "…"
            for t in titles[:want]]


def publish_card(job, chaps, total, work: Path, out: Path, tags: str) -> str:
    """Весь комплект одним текстом. Порядок — как человек заполняет формы."""
    import covers
    rules = load_publish_rules()
    y = job.get("youtube") or {}
    parts = []

    def block(head, body):
        body = (body or "").strip()
        if body:
            parts.append(f"───── {head} ─────\n{body}")

    block("ЗАГОЛОВОК", y.get("title", ""))
    block("ЗАПАСНЫЕ ЗАГОЛОВКИ",
          "\n".join(f"- {t}" for t in y.get("title_alternatives", [])))
    block("ОПИСАНИЕ (с тайм-кодами)", description(job, chaps, total))
    block("ИСТОЧНИКИ", sources_block(job, work, rules))
    block(f"ХЕШТЕГИ ({len(hashtags_five(job, rules))})",
          " ".join(hashtags_five(job, rules)))
    block(f"ТЕГИ ({len(tags)} из {TAGS_LIMIT} символов)", tags)
    block("НАЗВАНИЯ ШОРТСОВ",
          "\n".join(f"{i}. {t}" for i, t in
                    enumerate(short_titles(job, out, rules), 1)))
    try:
        prompts = covers.art_prompts(job, n=covers.COVER_COUNT)
        block("ПРОМПТ ОБЛОЖКИ",
              "\n\n".join(f"[вариант {chr(64 + i)}]\n{p}"
                          for i, p in enumerate(prompts, 1)))
    except Exception as e:                       # промпт — не повод ронять выкладку
        block("ПРОМПТ ОБЛОЖКИ", f"(собрать не вышло: {e})")
    block("ПЕРВЫЙ КОММЕНТАРИЙ",
          _fill(y.get("first_comment") or rules.get("first_comment", ""), job))
    block("ЗАПИСЬ ДЛЯ СООБЩЕСТВА",
          _fill(y.get("community_post") or rules.get("community_post", ""), job))
    cprompts = (y.get("community_prompts")
                or rules.get("community_prompts") or [])
    block("ПРОМПТЫ КАРТИНОК ДЛЯ СООБЩЕСТВА",
          "\n\n".join(f"[{i}]\n{_fill(str(p), job)}"
                      for i, p in enumerate(cprompts[:2], 1)))
    return "\n\n".join(parts) + "\n"


def thumbnail(video: Path, out: Path, at: float, title: str, style="lower_left"):
    """
    Кадр из самого ролика плюс заголовок. Ничего дорисованного.

    РАСКЛАДКА МЕНЯЕТСЯ ОТ РОЛИКА К РОЛИКУ. YouTube показывает превью соседних
    загрузок канала в одном ряду, и одинаковая вёрстка подписи опознаётся как
    поточная серия быстрее, чем любой признак внутри самого ролика. Вариант
    выбирает движок стиля по seed, то есть он свой у каждого ролика и при
    этом воспроизводимый.

      lower_left   подпись внизу слева, затемнение снизу  — крупно, спокойно
      lower_band   подпись в плашке во всю ширину внизу   — плотно, «газета»
      upper_left   подпись сверху слева, затемнение сверху — под кадры,
                   где главное в нижней половине
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    raw = out.parent / "_thumb_raw.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
                    "-frames:v", "1", "-y", str(raw)], check=True)

    im = Image.open(raw).convert("RGB").resize((W_THUMB, H_THUMB), Image.LANCZOS)
    top = style == "upper_left"

    if style == "lower_band":
        # плашка: сплошная полоса, текст на ней всегда читается независимо
        # от того, что попало в кадр
        band = Image.new("RGB", im.size, (0, 0, 0))
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rectangle([0, H_THUMB - 210, W_THUMB, H_THUMB],
                                       fill=205)
        im = Image.composite(band, im, mask.filter(ImageFilter.GaussianBlur(2)))
    else:
        # градиент от края: мягче, но зависит от содержимого кадра
        shade = Image.new("L", (W_THUMB, H_THUMB), 0)
        sd = ImageDraw.Draw(shade)
        half = H_THUMB // 2
        for i in range(half):
            k = i / half
            y = half - 1 - i if top else half + i
            sd.line([(0, y), (W_THUMB, y)], fill=int(215 * k ** 1.4))
        im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im,
                             shade.filter(ImageFilter.GaussianBlur(8)))

    d = ImageDraw.Draw(im)
    size = 54 if style == "lower_band" else 62
    font = ImageFont.truetype(FONT_BOLD, size)
    step = size + 12
    margin = 46 if style == "lower_band" else 60

    lines, cur = [], ""
    for w in title.split():
        probe = (cur + " " + w).strip()
        if d.textlength(probe, font=font) > W_THUMB - margin * 2 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = probe
    lines.append(cur)

    if top:
        y = 52
    elif style == "lower_band":
        y = H_THUMB - 40 - len(lines) * step
    else:
        y = H_THUMB - 56 - len(lines) * step
    for ln in lines:
        d.text((margin, y), ln, font=font, fill=(240, 238, 232))
        y += step

    im.save(out, quality=92)
    raw.unlink(missing_ok=True)
    return out


def main(job_path):
    job = load_job(job_path)
    if "youtube" not in job:
        raise SystemExit("в спецификации нет блока youtube — заполнять нечего")

    out = Path("work") / job["id"] / "out"
    srt, video = out / "subs.srt", out / "final.mp4"
    for f in (srt, video):
        if not f.exists():
            raise SystemExit(f"нет {f} — сначала собери ролик")

    total = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout)

    chaps = chapters(job, read_srt(srt))
    y = job["youtube"]

    tags = ", ".join(as_list(y.get("tags"), "tags"))
    if len(tags) > TAGS_LIMIT:
        raise SystemExit(f"теги занимают {len(tags)} символов при лимите {TAGS_LIMIT}")

    card = out / "youtube.txt"
    card.write_text(
        publish_card(job, chaps, total, Path("work") / job["id"], out, tags),
        encoding="utf-8")

    # Секунда превью обрезается длиной ролика. thumbnail_at пишется под
    # получасовой ролик (900 — пятнадцатая минута), а на тестовой сборке в
    # три минуты ffmpeg просто не находит там кадра: файл пустой, PIL падает
    # на «cannot identify image file» — по симптому не догадаешься.
    at = float(y.get("thumbnail_at", total * 0.35))
    if not 0 <= at < total:
        at = total * 0.35
        log(f"  thumbnail_at за пределами ролика, беру {at:.0f} сек")
    # Раскладку превью выбрал движок стиля при сборке — берём её оттуда,
    # чтобы не бросать жребий второй раз и не получить у одного ролика
    # разные превью при перезапуске этого шага.
    style_card = out / "style.json"
    thumb_style = "lower_left"
    if style_card.exists():
        thumb_style = json.loads(style_card.read_text(encoding="utf-8")).get(
            "thumb_style", thumb_style)
    # ХОЗЯИН thumbnail.jpg — covers.py, если он уже отработал. Он делает две
    # обложки с крупным белым заголовком слева и кладёт первую сюда же.
    # Здесь остался кадровый вариант со сменной раскладкой: он ничего не
    # стоит и работает без ключей, но перетирать им готовые обложки нельзя —
    # порядок шагов в workflow не должен решать, какое превью уедет к
    # человеку.
    if (out / "cover_1.jpg").exists():
        thumb = out / "thumbnail.jpg"
        log("превью : уже сделано covers.py (две обложки), не трогаю")
    else:
        thumb = thumbnail(video, out / "thumbnail.jpg", at, y["title"],
                          thumb_style)
        log(f"превью : раскладка {thumb_style}")

    log(f"главы  : {len(chaps)}, первая с 00:00, последняя с {stamp(chaps[-1][0])}")
    log(f"теги   : {len(tags)} символов из {TAGS_LIMIT}")
    log(f"описание и заголовок: {card}")
    log(f"превью : {thumb} ({thumb.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main(sys.argv[1])
