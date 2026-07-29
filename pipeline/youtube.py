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


# Подписи в описании. Канал русскоязычный, поэтому и умолчания русские;
# английский ролик переопределяет их полем description_labels, не трогая код.
LABELS = {"chapters": "Главы", "runtime": "Хронометраж"}


def description(job, chaps, total):
    y = job["youtube"]
    lab = {**LABELS, **(y.get("description_labels") or {})}
    parts = [y["description_intro"].strip(), "", lab["chapters"], ""]
    parts += [f"{stamp(t)}  {name}" for t, name in chaps]
    if y.get("description_notes"):
        parts += ["", y["description_notes"].strip()]
    parts += ["", f"{lab['runtime']}: {stamp(total)}"]
    if y.get("hashtags"):
        parts += ["", " ".join(y["hashtags"])]
    return "\n".join(parts)


def thumbnail(video: Path, out: Path, at: float, title: str):
    """Кадр из самого ролика плюс заголовок. Ничего дорисованного."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    raw = out.parent / "_thumb_raw.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
                    "-frames:v", "1", "-y", str(raw)], check=True)

    im = Image.open(raw).convert("RGB").resize((W_THUMB, H_THUMB), Image.LANCZOS)

    # затемняем низ, чтобы текст читался на любой картинке
    shade = Image.new("L", (W_THUMB, H_THUMB), 0)
    sd = ImageDraw.Draw(shade)
    for i in range(H_THUMB // 2, H_THUMB):
        k = (i - H_THUMB // 2) / (H_THUMB / 2)
        sd.line([(0, i), (W_THUMB, i)], fill=int(215 * k ** 1.4))
    im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im,
                         shade.filter(ImageFilter.GaussianBlur(8)))

    d = ImageDraw.Draw(im)
    words, lines, cur = title.split(), [], ""
    font = ImageFont.truetype(FONT_BOLD, 62)
    for w in words:
        probe = (cur + " " + w).strip()
        if d.textlength(probe, font=font) > W_THUMB - 120 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = probe
    lines.append(cur)

    y = H_THUMB - 56 - len(lines) * 74
    for ln in lines:
        d.text((60, y), ln, font=font, fill=(240, 238, 232))
        y += 74

    im.save(out, quality=92)
    raw.unlink(missing_ok=True)
    return out


def main(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
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

    tags = ", ".join(y.get("tags") or [])
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
    thumb = thumbnail(video, out / "thumbnail.jpg", at, y["title"])

    log(f"главы  : {len(chaps)}, первая с 00:00, последняя с {stamp(chaps[-1][0])}")
    log(f"теги   : {len(tags)} символов из {TAGS_LIMIT}")
    log(f"описание и заголовок: {card}")
    log(f"превью : {thumb} ({thumb.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main(sys.argv[1])
