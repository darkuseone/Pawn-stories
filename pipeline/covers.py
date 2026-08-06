"""
covers.py — две обложки к готовому ролику.

    python pipeline/covers.py jobs/<id>.json

Кладёт рядом с роликом cover_1.jpg, cover_2.jpg. Заголовок на обеих стоит
СЛЕВА крупными белыми буквами — это постоянная канала, а меняется под ним
фон.

Откуда берётся фон
------------------
Оба варианта рисует xAI по промпту. Если генерация недобрала до
COVER_COUNT (нет ключа, кончилась квота, сработал фильтр содержания) —
недостающее закрывается кадром из самого ролика. Это страховка, а не
третий равноправный вариант: обложка обязана появиться всегда, даже если
генерация в этот день не отвечает, а пустая папка обложек после сорока
минут рендера — худший исход из возможных.

Почему текст рисуется здесь, а не моделью
-----------------------------------------
Заказ был «грок умеет обложки с текстом». Умеет, но не гарантирует: модели
регулярно путают буквы, теряют пробелы и дописывают лишние слова, и заметно
это только на готовой картинке. Поэтому модель рисует ФОН с пустым левым
краем, а заголовок кладётся поверх шрифтом — тогда он всегда написан
правильно, всегда одного кегля и всегда читается на превью в ленте, где
картинка занимает сантиметр экрана.

Хотите попробовать текст самой моделью — поле cover_text_by_model в
спецификации: тогда заголовок уходит в промпт, а поверх ничего не
рисуется.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job

ROOT = Path(__file__).parent.parent
XAI = "https://api.x.ai/v1"

W, H = 1280, 720
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"

# Обложек две, не три. Раньше был третий, кадровый вариант — страховка на
# случай, если генерация вообще не отвечает (ни ключа, ни квоты, ни фильтр
# содержания). Он остаётся ЗАПАСНЫМ путём и добирает только то, чего не
# хватило генерации, но целевое число теперь два: сток-кадр рядом с двумя
# нарисованными фонами выглядит чужеродно — другая композиция, нет пустого
# места под заголовок, и разница видна на глаз.
COVER_COUNT = 2

# Текст занимает левую часть кадра. 0.56 — предел, за которым заголовок
# начинает лезть на смысловой центр картинки; проверено на трёх раскладках.
TEXT_ZONE = 0.56
MARGIN = 58


def log(*a):
    print(*a, flush=True)


def art_prompts(job, n=2):
    """
    Промпты фона. Левый край СПЕЦИАЛЬНО пустой — туда ляжет заголовок.

    Разные промпты, а не один и тот же дважды: две обложки с одинаковой
    композицией не дают выбора, ради которого их и делают.
    """
    y = job.get("youtube") or {}
    topic = (job.get("topic") or {}).get("slug", "") or y.get("title", "")
    base = ("dark moody antique shop scene, warm amber lamp light, aged wood "
            "and brass, cinematic photography, shallow depth of field, "
            "high contrast, 16:9 horizontal")
    tail = ("IMPORTANT: keep the LEFT THIRD of the frame dark, empty and "
            "uncluttered — no text, no letters, no words, no watermark, no "
            "logo. The subject sits on the RIGHT side of the frame.")
    variants = [
        f"{base}, close-up of a single mysterious object on a workbench, "
        f"subject on the right. Theme: {topic}. {tail}",
        f"{base}, wide shot of a dim auction room with one spotlit item, "
        f"subject on the right. Theme: {topic}. {tail}",
        f"{base}, weathered hands holding an object under a lamp, hands on "
        f"the right. Theme: {topic}. {tail}",
    ]
    if job.get("cover_text_by_model"):
        title = y.get("title", "")
        variants = [
            v.replace(tail,
                      f'Render the exact headline text "{title}" in large '
                      f'bold white letters on the LEFT side of the frame, '
                      f'spelled exactly as given. No other text.')
            for v in variants]
    return variants[:n]


def generate_art(prompt: str, dst: Path, key: str, model: str) -> bool:
    """Один фон через xAI. Возвращает, получилось ли."""
    try:
        r = requests.post(f"{XAI}/images/generations", timeout=180,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model, "prompt": prompt, "n": 1})
        if r.status_code != 200:
            log(f"  ! фон не вышел: {r.status_code} {r.text[:140]}")
            return False
        url = r.json()["data"][0]["url"]
        dst.write_bytes(requests.get(url, timeout=120).content)
        return True
    except Exception as e:
        log(f"  ! фон не вышел: {e}")
        return False


def frame_from(video: Path, dst: Path, at: float) -> bool:
    """Кадр из ролика — страховочный фон, ни от чего не зависит."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}",
                        "-i", str(video), "-frames:v", "1", "-y", str(dst)],
                       capture_output=True)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 5000


def draw_title(bg: Path, dst: Path, title: str):
    """
    Заголовок крупными белыми буквами слева.

    Под текстом — растушёванная тень от левого края, а не плашка: плашка
    режет картинку пополам и на превью в ленте читается как баннер. Тень
    даёт тот же контраст и оставляет фон фоном.
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    im = Image.open(bg).convert("RGB").resize((W, H), Image.LANCZOS)

    # затемнение слева: сильное у края, сходит на нет к центру
    shade = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shade)
    edge = int(W * TEXT_ZONE)
    for x in range(edge):
        k = 1.0 - x / edge
        sd.line([(x, 0), (x, H)], fill=int(225 * k ** 0.85))
    im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im,
                         shade.filter(ImageFilter.GaussianBlur(14)))

    d = ImageDraw.Draw(im)
    box_w = int(W * TEXT_ZONE) - MARGIN * 2

    # Кегль подбирается под длину заголовка, а не задаётся числом: короткий
    # заголовок должен занимать кадр, длинный — влезать. Ищем самый крупный,
    # при котором текст укладывается в четыре строки.
    for size in range(96, 39, -4):
        font = ImageFont.truetype(FONT_BOLD, size)
        lines, cur = [], ""
        for w in title.split():
            probe = (cur + " " + w).strip()
            if d.textlength(probe, font=font) > box_w and cur:
                lines.append(cur)
                cur = w
            else:
                cur = probe
        if cur:
            lines.append(cur)
        step = size + 14
        if len(lines) <= 4 and len(lines) * step <= H - MARGIN * 2:
            break

    y = (H - len(lines) * step) // 2
    for ln in lines:
        # мягкая обводка: белый текст на тёмном фоне всё равно теряется на
        # светлых кадрах, а обводка держит его читаемым на любом
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            d.text((MARGIN + dx, y + dy), ln, font=font, fill=(0, 0, 0))
        d.text((MARGIN, y), ln, font=font, fill=(255, 255, 255))
        y += step

    im.save(dst, quality=92)
    return dst


def main(job_path):
    job = load_job(job_path)
    out = ROOT / "work" / job["id"] / "out"
    video = out / "final.mp4"
    if not video.exists():
        raise SystemExit(f"нет {video} — сначала собери ролик")

    y = job.get("youtube") or {}
    title = y.get("title") or job["id"]
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    model = job.get("image_model", "grok-imagine-image")

    total = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)], capture_output=True,
        text=True).stdout or 0)

    tmp = out / "_cover_bg"
    tmp.mkdir(exist_ok=True)
    backgrounds = []

    if key:
        log("── фоны обложек через xAI")
        for i, p in enumerate(art_prompts(job, n=COVER_COUNT), 1):
            dst = tmp / f"art_{i}.jpg"
            if dst.exists() or generate_art(p, dst, key, model):
                backgrounds.append(dst)
                log(f"  фон {i}: готов")
    else:
        log("  ! нет XAI_API_KEY — обложки будут только из кадров ролика")

    # Страховочные фоны кадрами — ТОЛЬКО если генерация недобрала до
    # COVER_COUNT. Берутся на разных долях ролика, чтобы не быть двумя
    # видами одной сцены.
    if len(backgrounds) < COVER_COUNT:
        log("── страховочные фоны кадрами из ролика")
        for i, frac in enumerate((0.35, 0.62, 0.18), 1):
            if len(backgrounds) >= COVER_COUNT:
                break
            dst = tmp / f"frame_{i}.jpg"
            if frame_from(video, dst, total * frac):
                backgrounds.append(dst)
                log(f"  кадр на {total*frac:.0f} с")

    if not backgrounds:
        raise SystemExit("не вышло ни одного фона: ни генерации, ни кадра")

    made = []
    by_model = bool(job.get("cover_text_by_model"))
    for i, bg in enumerate(backgrounds[:COVER_COUNT], 1):
        dst = out / f"cover_{i}.jpg"
        if by_model:
            # текст уже нарисовала модель — только приводим к размеру
            from PIL import Image
            Image.open(bg).convert("RGB").resize((W, H),
                                                 Image.LANCZOS).save(dst, quality=92)
        else:
            draw_title(bg, dst, title)
        made.append(dst)
        log(f"  {dst.name}: {dst.stat().st_size // 1024} КБ")

    # Первая обложка — она же thumbnail.jpg: у YouTube одно превью, вторая
    # лежит рядом как вариант для ручной замены.
    main_thumb = out / "thumbnail.jpg"
    if made:
        main_thumb.write_bytes(made[0].read_bytes())
    log(f"── обложек готово: {len(made)} (первая продублирована в "
        f"{main_thumb.name})")
    return made


if __name__ == "__main__":
    main(sys.argv[1])
