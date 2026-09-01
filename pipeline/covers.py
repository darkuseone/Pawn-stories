"""
covers.py — две обложки к готовому ролику.

    python pipeline/covers.py jobs/<id>.json
    python pipeline/covers.py --check jobs/<id>.json

Постоянная канала: левая треть почти чёрная, белый ультражирный гротеск
Oswald Bold, крючок 3–5 слов + короткая вторая строка с цифрой, если она
есть. Меняется только фон. Обе рисует xAI. Кадр из ролика — не третий
равноправный вариант, а страховка: включается, только если генерация
недобрала до COVER_COUNT.

CTR правой половины
-------------------
Большинство кликов — с телефона, где превью ~120 px (suggested / search /
up-next), не холст 1280. На таком размере выживает один огромный предмет
на ПРАВОЙ половине и жёсткий контраст по яркости. Широкая лавка, галерея
или чердак с крошечной вещью на столе в ленте читаются как шум.

Правила лежат в channel/covers.json и дописываются к каждому промпту
(и к youtube.cover_prompts, и к запасным). Сюжет ролика — из ключевых
слов и глав, не универсальный «antique shop». Два варианта: дыра/загадка
и масштаб/сделка — не два ракурса одной комнаты.

Канал без ведущего: героем кадра tension object (пустая рама, печать,
коробка, треснувшая доска), не лицо блогера. YouTube Test & Compare
выбирает победителя по watch time, не по сырому CTR — в картинку не
класть ответ и не рисовать суммы: сумма живёт в cover_sub.

Почему текст рисуется здесь, а не моделью
-----------------------------------------
Модель путает буквы, теряет пробелы и дописывает лишние слова, и видно
это только на готовой картинке. Поэтому модель рисует ФОН с пустым левым
краем, а заголовок кладётся поверх шрифтом канала. Поле cover_text_by_model
больше не обходит эту отрисовку: сравнение «текст от модели» слишком легко
оставляет в ленте нечитаемые буквы.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job
import type as type_mod

ROOT = Path(__file__).parent.parent
COVER_RULES = ROOT / "channel" / "covers.json"
XAI = "https://api.x.ai/v1"

W, H = 1280, 720

# Обложек две, не три. Кадровый вариант остаётся ЗАПАСНЫМ путём и добирает
# только то, чего не хватило генерации.
COVER_COUNT = 2

# Текст занимает левую часть кадра. 0.56 — предел, за которым заголовок
# начинает лезть на смысловой центр картинки.
TEXT_ZONE = 0.56
MARGIN = 58

# Запас, если channel/covers.json нет в чекауте. Живые правила — в JSON.
CTR_MARKER = "CHANNEL COVER CTR:"
LEFT_THIRD = (
    "IMPORTANT: keep the LEFT THIRD of the frame dark, empty and uncluttered "
    "— no text, no letters, no words, no watermark, no logo. The subject sits "
    "on the RIGHT side of the frame, filling that half."
)


def log(*a):
    print(*a, flush=True)


def load_cover_rules() -> dict:
    """Правила CTR канала. Ключи с подчёркиванием — комментарии, не данные."""
    if not COVER_RULES.exists():
        return {}
    data = json.loads(COVER_RULES.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _marker(rules: dict) -> str:
    return str(rules.get("marker") or CTR_MARKER)


OBJECTISH = (
    "mark", "hallmark", "bowl", "egg", "frame", "coin", "penny", "jacket",
    "cartridge", "box", "seal", "panel", "painting", "print", "porcelain",
    "trunk", "gavel", "silver", "underside", "nes", "mona", "salvator",
    "faberge", "durer", "ming", "sticker", "receipt", "peg",
)


def _kw_usable(kw: str, skip: list[str]) -> bool:
    low = (kw or "").strip().lower()
    if len(low) < 4:
        return False
    skip_set = {s.strip().lower() for s in skip if s}
    if low in skip_set:
        return False
    return True


def _kw_score(kw: str, kicker: str = "") -> int:
    """Длинное имя вещи важнее абстракции («habit», «market»)."""
    low = kw.lower()
    words = low.split()
    score = len(words) * 10 + min(len(kw), 40)
    if any(tok in low for tok in OBJECTISH):
        score += 50
    if any(w in {"habit", "value", "market", "records", "lawsuit"} for w in words):
        score -= 35
    kick = {w.lower() for w in (kicker or "").split() if len(w) > 2}
    if kick and any(w in low for w in kick):
        score += 25
    return score


def visual_hooks(job, rules=None) -> tuple[str, str]:
    """
    Два разных героя правой половины: из ключевых слов, глав, крючка.

    Короткие абстракции («reverse», «auction records») пропускаются —
    модель из них рисует витрину. Нужны имена и вещи: рама, печать, коробка.
    """
    rules = rules if rules is not None else load_cover_rules()
    skip = [str(s) for s in (rules.get("skip_keywords") or [])]
    kicker, sub = type_mod.cover_lines(job)
    kws = list((job.get("topic") or {}).get("keywords") or [])
    ranked = sorted(
        (str(k).strip() for k in kws if _kw_usable(str(k), skip)),
        key=lambda s: _kw_score(s, kicker), reverse=True)
    chapters = [str(c).strip() for c in ((job.get("youtube") or {}).get("chapters") or [])
                if str(c).strip()]
    extra = [c for c in chapters if _kw_usable(c, skip)]
    pool = ranked + extra
    if kicker:
        pool.append(kicker.lower())
    if sub:
        pool.append(sub.lower())
    gap = pool[0] if pool else (kicker or "empty ornate picture frame")
    scale = next((p for p in pool[1:] if p.lower() != gap.lower()), "")
    if not scale:
        scale = chapters[min(2, len(chapters) - 1)] if chapters else (
            "one large authentic artifact, extreme close-up")
    return gap, scale


def _apply_ctr(scene: str, variant: str, rules: dict) -> str:
    """Дописать блок CTR, если его ещё нет. Сюжет не переписывается."""
    scene = (scene or "").strip()
    mark = _marker(rules)
    if mark in scene:
        return scene
    gap = (rules.get("variant_gap") or "").strip()
    scale = (rules.get("variant_scale") or "").strip()
    crop = (rules.get("crop_override") or "").strip()
    block = (rules.get("constraint_block") or LEFT_THIRD).strip()
    recipe = gap if variant == "gap" else scale
    bits = [scene, crop, recipe, block]
    return " ".join(b.strip() for b in bits if b and b.strip())


def _auto_scenes(job, rules: dict) -> list[str]:
    """Запасной сюжет. Черновик _превью_промпт.prompt не берём: там часто
    широкая комната, а не герой на правую половину."""
    topic = ((job.get("topic") or {}).get("slug") or
             (job.get("youtube") or {}).get("title") or job.get("id") or "")
    gap, scale = visual_hooks(job, rules)
    a = (
        f"Hyperrealistic 16:9 YouTube thumbnail. Curiosity-gap still of "
        f"{gap}: ONE giant tension object filling the right half of the "
        f"frame, extreme close crop. Dark empty left. Theme: {topic}."
    )
    b = (
        f"Hyperrealistic 16:9 YouTube thumbnail. A different story from the "
        f"first cover: {scale} as a giant right-half close-up — gloved hands "
        f"and the artifact, or the object itself filling the frame. Not the "
        f"same room. Theme: {topic}."
    )
    return [a, b]


def art_prompts(job, n=2):
    """
    Промпты фона. Левый край СПЕЦИАЛЬНО пустой — туда ляжет заголовок.

    youtube.cover_prompts — ровно два разных сюжета. Не два ракурса одной
    лавки: вариант A — визуальная дыра/загадка, вариант B — другой крючок
    (крупный предмет, руки, торг). Тема из topic / глав, не универсальный
    «antique shop». Блок CTR из channel/covers.json дописывается всегда.
    """
    rules = load_cover_rules()
    y = job.get("youtube") or {}
    specified = y.get("cover_prompts")
    scenes = []
    if isinstance(specified, list):
        scenes = [str(p).strip() for p in specified if str(p).strip()]
    if len(scenes) < n:
        auto = _auto_scenes(job, rules)
        scenes = (scenes + auto)[:n] if scenes else auto
    variants = ("gap", "scale")
    out = []
    for i in range(n):
        scene = scenes[i] if i < len(scenes) else scenes[-1]
        out.append(_apply_ctr(scene, variants[i % 2], rules))
    return out


def check_prompts(prompts, rules=None) -> list[str]:
    """Дешёвая проверка готовых промптов. Пустой список — порядок."""
    rules = rules if rules is not None else load_cover_rules()
    mark = _marker(rules)
    problems = []
    if len(prompts) != COVER_COUNT:
        problems.append(f"промптов {len(prompts)}, нужно {COVER_COUNT}")
        return problems
    if prompts[0].strip() == prompts[1].strip():
        problems.append("два одинаковых сюжета обложки")
    blob = " ".join(prompts).lower()
    if mark.lower() not in blob:
        problems.append("нет блока CHANNEL COVER CTR")
    if "120" not in blob:
        problems.append("в CTR-блоке нет правила 120 px")
    if "bottom-right" not in blob and "bottom right" not in blob:
        problems.append("нет предупреждения про шильдик длительности")
    return problems


def check_job_covers(job) -> list[str]:
    """Промпты + крючок. Для смоука и `covers.py --check`."""
    problems = []
    kicker, _sub = type_mod.cover_lines(job)
    if not kicker:
        problems.append("пустой cover_kicker")
    prompts = art_prompts(job, n=COVER_COUNT)
    problems.extend(check_prompts(prompts))
    return problems


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


def draw_title(bg: Path, dst: Path, kicker: str, sub: str = ""):
    """
    Крючок крупными белыми буквами слева, Oswald Bold, all-caps.

    Под текстом — растушёванная тень от левого края, а не плашка: плашка
    режет картинку пополам и на превью в ленте читается как баннер.
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    im = Image.open(bg).convert("RGB").resize((W, H), Image.LANCZOS)

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
    path = str(type_mod.font_path())
    kicker = " ".join((kicker or "").split()).upper()
    sub = " ".join((sub or "").split()).upper()

    size = 104
    lines = []
    for size in range(104, 43, -4):
        font = ImageFont.truetype(path, size)
        lines, cur = [], ""
        for w in kicker.split():
            probe = (cur + " " + w).strip()
            if d.textlength(probe, font=font) > box_w and cur:
                lines.append(cur)
                cur = w
            else:
                cur = probe
        if cur:
            lines.append(cur)
        # плотный интерлиньяж: all-caps Oswald и так высокий
        step = int(size * 1.06)
        sub_h = int(size * 0.48) + 16 if sub else 0
        if len(lines) <= 4 and len(lines) * step + sub_h <= H - MARGIN * 2:
            break

    font = ImageFont.truetype(path, size)
    step = int(size * 1.06)
    sub_size = max(28, int(size * 0.42))
    sub_font = ImageFont.truetype(path, sub_size) if sub else None
    sub_lines = []
    if sub and sub_font:
        cur = ""
        for w in sub.split():
            probe = (cur + " " + w).strip()
            if d.textlength(probe, font=sub_font) > box_w and cur:
                sub_lines.append(cur)
                cur = w
            else:
                cur = probe
        if cur:
            sub_lines.append(cur)

    block_h = len(lines) * step + (len(sub_lines) * int(sub_size * 1.12) + 18
                                   if sub_lines else 0)
    y = (H - block_h) // 2

    def stamp(text, font, y, fill=(255, 255, 255)):
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
            d.text((MARGIN + dx, y + dy), text, font=font, fill=(0, 0, 0))
        d.text((MARGIN, y), text, font=font, fill=fill)
        return y + int(font.size * 1.06)

    for ln in lines:
        y = stamp(ln, font, y)
    if sub_lines:
        y += 10
        for ln in sub_lines:
            y = stamp(ln, sub_font, y)

    im.save(dst, quality=92)
    return dst


def kicker_fits(job) -> bool:
    """Смоук: крючок влезает в TEXT_ZONE не более чем в четыре строки."""
    from PIL import ImageFont
    kicker, _sub = type_mod.cover_lines(job)
    path = str(type_mod.font_path())
    if not Path(path).exists():
        return False
    box_w = int(W * TEXT_ZONE) - MARGIN * 2
    font = ImageFont.truetype(path, 72)
    lines, cur = [], ""
    for w in kicker.split():
        probe = (cur + " " + w).strip()
        if font.getlength(probe) > box_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = probe
    if cur:
        lines.append(cur)
    return 1 <= len(lines) <= 4 and bool(kicker)


def main(job_path):
    job = load_job(job_path)
    out = ROOT / "work" / job["id"] / "out"
    video = out / "final.mp4"
    if not video.exists():
        raise SystemExit(f"нет {video} — сначала собери ролик")

    kicker, sub = type_mod.cover_lines(job)
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
    log(f"── текст обложки: «{kicker}»" + (f" / «{sub}»" if sub else ""))
    if job.get("cover_text_by_model"):
        log("  ! cover_text_by_model игнорируется — текст рисует шрифт канала")
    for i, bg in enumerate(backgrounds[:COVER_COUNT], 1):
        dst = out / f"cover_{i}.jpg"
        draw_title(bg, dst, kicker, sub)
        made.append(dst)
        log(f"  {dst.name}: {dst.stat().st_size // 1024} КБ")

    main_thumb = out / "thumbnail.jpg"
    if made:
        main_thumb.write_bytes(made[0].read_bytes())
    log(f"── обложек готово: {len(made)} (первая продублирована в "
        f"{main_thumb.name})")
    return made


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        job = load_job(sys.argv[2])
        problems = check_job_covers(job)
        kicker, sub = type_mod.cover_lines(job)
        print(f"kicker: {kicker}" + (f" / {sub}" if sub else ""))
        for i, p in enumerate(art_prompts(job), 1):
            print(f"--- prompt {i} ({len(p)} chars) ---")
            print(p)
        if problems:
            raise SystemExit("обложки: " + "; ".join(problems))
        print("CTR-промпты в порядке")
        raise SystemExit(0)
    main(sys.argv[1])
