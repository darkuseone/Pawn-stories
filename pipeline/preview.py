"""
preview.py — лист отбора материала.

Между скачиванием и монтажом стоит человек. Робот приносит всё, что нашёл
по запросам, но что из этого годится ролику — решать не ему: в подборку
регулярно попадают морг по запросу «лаборатория», кабина самолёта по
запросу «центр управления» и заставка чужого фильма из хроники.

  python pipeline/preview.py jobs/lhc-01.json

Кладёт в work/<id>/out два листа с пронумерованными кадрами — отдельно
футаж, отдельно архивные фото. Номер на кадре это номер в списке отказов.
Дальше в спецификацию ролика дописывается

    "reject": { "clip": [3, 7], "arch": [1, 12] }

и монтаж эти файлы не берёт. Сами файлы остаются на месте: их не удаляют,
чтобы отказ можно было отменить, не выкачивая всё заново.
"""

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLS = 5
CELL_W, CELL_H = 384, 216
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def log(*a):
    print(*a, flush=True)


def index_of(path: Path) -> int:
    """clip_007_pexels.mp4 -> 7. Номер стоит в имени с момента скачивания."""
    return int(path.name.split("_")[1])


def frame_of(path: Path, cell: Image.Image = None):
    """Кадр из видео или сама картинка, ужатые под ячейку листа."""
    if path.suffix.lower() in (".mp4", ".m4v"):
        tmp = path.with_suffix(".preview.png")
        # берём кадр с середины: первые кадры у стоков часто чёрные
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        at = max(0.0, float(dur.strip() or 2) / 2)
        subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(path),
                        "-frames:v", "1", "-y", str(tmp)], check=True)
        im = Image.open(tmp).convert("RGB")
        tmp.unlink(missing_ok=True)
    else:
        im = Image.open(path).convert("RGB")
    return im.resize((CELL_W, CELL_H), Image.LANCZOS)


def pale_share(im: Image.Image) -> float:
    """
    Какая доля кадра — почти белое.

    Музейные и библиотечные каталоги снимают предмет на белом фоне. Само
    фото при этом отличное и предмет настоящий, но на тёплом тёмном
    цветокоре канала такой кадр становится бледным прямоугольником и
    выбивается из ролика — на прогоне pawn-01 таких набралось четыре из
    двадцати пяти, и заметно это стало только на готовом видео.

    Здесь это не отказ, а пометка: решает человек. Кадр с белым фоном
    иногда именно то, что нужно, — например, единственное существующее
    изображение предмета, о котором идёт речь.
    """
    small = im.convert("L").resize((64, 36))
    # getdata() объявлен устаревшим в Pillow и на каждый вызов печатает
    # предупреждение. На сотне файлов это двести строк мусора в логе, из-за
    # которых полезный вывод отбраковки уезжает за границу видимости —
    # я дважды вытаскивал логи вслепую именно поэтому.
    try:
        px = list(small.get_flattened_data())
    except AttributeError:
        px = list(small.getdata())
    return sum(1 for p in px if p > 224) / len(px)


PALE_LIMIT = 0.45


def sheet(files, out: Path, kind: str, rejected):
    if not files:
        log(f"  {kind}: пусто")
        return None
    rows = (len(files) + COLS - 1) // COLS
    im = Image.new("RGB", (CELL_W * COLS, CELL_H * rows), (12, 12, 14))
    d = ImageDraw.Draw(im)
    big = ImageFont.truetype(FONT, 34)
    small = ImageFont.truetype(FONT, 17)

    pale = []
    for i, f in enumerate(files):
        x, y = (i % COLS) * CELL_W, (i // COLS) * CELL_H
        try:
            cell = frame_of(f)
            im.paste(cell, (x, y))
            if pale_share(cell) > PALE_LIMIT:
                pale.append(index_of(f))
                # жёлтая рамка: не отказ, а «посмотри внимательнее»
                d.rectangle([x + 2, y + 2, x + CELL_W - 3, y + CELL_H - 3],
                            outline=(235, 200, 70), width=3)
                d.text((x + CELL_W - 150, y + CELL_H - 30), "белый фон",
                       font=small, fill=(235, 200, 70))
        except Exception as e:
            d.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(40, 20, 20))
            d.text((x + 12, y + 90), f"не открылся\n{e}"[:60], font=small)

        n = index_of(f)
        out_of = n in rejected
        if out_of:                       # отклонённые гасим и перечёркиваем
            dark = Image.new("RGB", (CELL_W, CELL_H), (0, 0, 0))
            im.paste(Image.blend(im.crop((x, y, x + CELL_W, y + CELL_H)), dark, 0.72), (x, y))
            d.line([x + 20, y + 20, x + CELL_W - 20, y + CELL_H - 20], fill=(220, 60, 60), width=5)

        d.rectangle([x + 6, y + 6, x + 74, y + 48], fill=(0, 0, 0))
        d.text((x + 16, y + 10), f"{n:02d}", font=big,
               fill=(220, 70, 70) if out_of else (250, 240, 150))
        src = f.stem.split("_", 2)[-1]
        d.text((x + 84, y + 18), src[:26], font=small, fill=(200, 200, 200))

    im.save(out, quality=88)
    log(f"  {kind}: {len(files)} шт -> {out.name}")
    if pale:
        log(f"    жёлтым обведено {len(pale)} на белом фоне: {sorted(pale)}")
        log(f"    на тёмном тёплом грейде они станут бледными прямоугольниками")
    return out


def main(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    base = Path("work") / job["id"]
    assets, out = base / "assets", base / "out"
    out.mkdir(parents=True, exist_ok=True)

    rej = job.get("reject") or {}
    clips = sorted((assets / "footage").glob("clip_*.mp4"))
    arch = sorted((assets / "archive").glob("arch_*.jpg"))

    sheet(clips, out / "select_footage.jpg", "футаж", set(rej.get("clip", [])))
    sheet(arch, out / "select_archive.jpg", "архив", set(rej.get("arch", [])))

    log(f"\nОтклонить материал — дописать в {job_path}:")
    log('  "reject": { "clip": [номера], "arch": [номера] }')
    log(f"сейчас отклонено: футаж {sorted(rej.get('clip', []))}, "
        f"архив {sorted(rej.get('arch', []))}")


if __name__ == "__main__":
    main(sys.argv[1])
