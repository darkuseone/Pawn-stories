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

from jobspec import load_job

W_THUMB, H_THUMB = 1280, 720
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
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

    Ищем по первому предложению блока: оно уникально и всегда попадает в
    отдельную реплику, потому что субтитры режутся ровно по предложениям.
    """
    names = job["youtube"]["chapters"]
    blocks = job["script_blocks"]
    if len(names) != len(blocks):
        raise SystemExit(f"глав {len(names)}, а блоков сценария {len(blocks)} — "
                         "их должно быть поровну")

    out = []
    for i, (block, name) in enumerate(zip(blocks, names)):
        key = norm(block.strip().split(".")[0])[:45]
        hit = next((t for t, txt in cues if key and key in txt), None)
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
    hashtags = as_list(y.get("hashtags"), "hashtags")
    if hashtags:
        parts += ["", " ".join(hashtags)]
    return "\n".join(parts)


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
        "ЗАГОЛОВОК\n" + y["title"] +
        "\n\nЗАПАСНЫЕ ЗАГОЛОВКИ\n" +
        "\n".join(f"- {t}" for t in y.get("title_alternatives", [])) +
        "\n\nОПИСАНИЕ\n" + description(job, chaps, total) +
        f"\n\nТЕГИ ({len(tags)} из {TAGS_LIMIT} символов)\n" + tags + "\n",
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
    # ХОЗЯИН thumbnail.jpg — covers.py, если он уже отработал. Он делает три
    # обложки с крупным белым заголовком слева и кладёт первую сюда же.
    # Здесь остался кадровый вариант со сменной раскладкой: он ничего не
    # стоит и работает без ключей, но перетирать им готовые обложки нельзя —
    # порядок шагов в workflow не должен решать, какое превью уедет к
    # человеку.
    if (out / "cover_1.jpg").exists():
        thumb = out / "thumbnail.jpg"
        log("превью : уже сделано covers.py (три обложки), не трогаю")
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
