"""
overlays.py — генерирует зацикленный слой искр.

Запускается ОДИН раз, результат лежит в репозитории. При монтаже робот
просто накладывает готовый файл, а не считает частицы заново — иначе
рендер вырос бы вдвое.

Искры летят вверх со скоростью 80-120 пикселей в секунду и часто мерцают.
Размер 1-3 пикселя, на стоп-кадре их почти не видно — работают они в
движении.

Яркость печётся здесь на полную, а СИЛА наложения задаётся отдельно при
склейке (spark_opacity в style.py). Так у плотности и у прозрачности по
одной ручке на каждую, а не одна перемноженная на другую.

Генератор дымки make_haze ниже оставлен, но в монтаж не входит.
"""

import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# Частоту берём из render: слой искр обязан идти с той же частотой,
# что и кадр, иначе ffmpeg будет дублировать кадры при наложении
# и ровное движение начнёт подрагивать.
import sys
sys.path.insert(0, str(Path(__file__).parent))
from render import W, H, FPS

# Три варианта плотности на выбор движка стиля
# Считаны с запасом: период оборота больше высоты кадра, поэтому
# часть искр в каждый момент находится за экраном.
SPARK_COUNTS = (34, 46, 58)


def make_sparks(out: Path, seconds=20, count=30, seed=1,
                size_range=(1.0, 3.0), px_sec=(80.0, 120.0),
                flicker=(4.0, 8.0)):
    """
    Угли, летящие вверх — как над костром. Быстрый подъём, лёгкий боковой
    снос, частое мерцание. Накладывается режимом screen.

    Скорость задаётся В ПИКСЕЛЯХ В СЕКУНДУ и проверяется линейкой: искра
    при 100 px/s пересекает кадр примерно за одиннадцать секунд.

    ПЕТЛЯ БЕСШОВНА ПО ПОСТРОЕНИЮ, это здесь принципиально и легко сломать.
    Каждая искра оборачивается по СВОЕМУ периоду period = v * seconds / k,
    где k целое. Тогда за длину петли она проходит ровно k периодов и
    возвращается в исходную точку. Период всегда берётся не меньше высоты
    кадра с запасом, поэтому сам момент оборота происходит за экраном и
    зритель его не видит. Боковой снос и мерцание — синусы с ЦЕЛЫМ числом
    периодов за петлю, они замыкаются по той же причине.

    Яркость печатается на полную: сила наложения живёт отдельно, в
    spark_opacity, чтобы ручка была одна, а не две перемноженные.
    """
    rng = random.Random(seed)
    frames = int(round(seconds * FPS))
    margin = 40
    min_period = H + 2 * margin
    tmp = out.parent / f"_sp_{seed}"
    tmp.mkdir(parents=True, exist_ok=True)

    parts = []
    for _ in range(count):
        v = rng.uniform(*px_sec)                 # пикселей в секунду, вверх
        # k — сколько раз искра обернётся за петлю. Берём floor, чтобы период
        # не оказался короче кадра: иначе оборот было бы видно прямо на экране.
        k = max(1, int(v * seconds / min_period))
        period = v * seconds / k
        # мерцание: радианы в секунду -> целое число периодов за петлю
        cycles = max(1, round(rng.uniform(*flicker) * seconds / math.tau))
        parts.append(dict(
            x0=rng.uniform(0, W), y0=rng.uniform(0, period),
            vy=-v / FPS,                         # пикселей за кадр
            period=period,
            amp=rng.uniform(5, 26),              # боковой снос
            cyc=rng.choice([1, 2, 3]),           # периодов сноса за петлю
            fl=cycles,                           # периодов мерцания за петлю
            ph=rng.uniform(0, math.tau),
            r=rng.uniform(*size_range),
            warm=rng.random() < 0.78,            # угли в основном тёплые
        ))

    for f in range(frames):
        u = f / frames                           # 0..1 по петле
        img = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(img)
        for p in parts:
            y = (p["y0"] + p["vy"] * f) % p["period"] - margin
            x = p["x0"] + p["amp"] * math.sin(math.tau * p["cyc"] * u + p["ph"])
            a = 0.30 + 0.70 * (0.5 + 0.5 * math.sin(math.tau * p["fl"] * u + p["ph"]))
            v = int(255 * a)
            col = (v, int(v * 0.60), int(v * 0.22)) if p["warm"] else \
                  (int(v * 0.72), int(v * 0.82), v)
            r = p["r"]
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        img.filter(ImageFilter.GaussianBlur(0.9)).save(tmp / f"{f:05d}.png")

    subprocess.run(
        f"ffmpeg -y -framerate {FPS} -i {tmp}/%05d.png -c:v libx264 -crf 26 "
        f"-preset veryfast -pix_fmt yuv420p {out}",
        shell=True, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()


# Дымка — размытое пятно без единой резкой детали, поэтому считается в
# уменьшенном разрешении, а до 1920x1080 её растягивает уже ffmpeg при
# склейке. На глаз отличий нет, а генерация быстрее примерно в девять раз:
# в полном разрешении она одна съедала половину времени сборки ролика.
HAZE_W, HAZE_H = 640, 360


def make_haze(out: Path, seconds=20, seed=1, opacity=0.6):
    """
    Ползущая дымка. Плавный шум, растянутый и размытый.

    В монтаж НЕ входит: дымку с канала убрали, она не понравилась ни
    медленной, ни быстрой, ни лёгкой. Генератор оставлен на случай, если
    к ней захотят вернуться — тогда сюда же вернутся haze_variant,
    haze_speed и haze_opacity в style.py и второй blend в build.py.
    """
    rng = np.random.default_rng(seed)
    frames = seconds * FPS
    w, h = HAZE_W, HAZE_H
    tmp = out.parent / f"_hz_{seed}"
    tmp.mkdir(parents=True, exist_ok=True)

    # три слоя шума разной крупности, чтобы не читался как рябь
    base = [rng.random((h // 14 + 2, w // 14 + 2)) for _ in range(3)]

    for f in range(frames):
        t = f / frames
        acc = np.zeros((h, w), np.float32)
        for i, b in enumerate(base):
            # циклический сдвиг: на стыке петли картинка совпадает сама с собой
            sh = int(t * b.shape[1])
            rolled = np.roll(b, sh + i * 3, axis=1)
            up = np.asarray(Image.fromarray((rolled * 255).astype(np.uint8))
                            .resize((w, h), Image.BICUBIC), np.float32) / 255
            acc += up * (0.5 ** i)
        acc /= acc.max()
        # дымка гуще внизу кадра
        grad = np.linspace(0.55, 1.35, h, dtype=np.float32)[:, None]
        acc = np.clip(acc * grad, 0, 1)
        px = (acc * 255 * opacity).astype(np.uint8)
        rgb = np.dstack([(px * 0.80).astype(np.uint8),
                         (px * 0.90).astype(np.uint8),
                         px])                      # синеватая дымка
        Image.fromarray(rgb).filter(ImageFilter.GaussianBlur(3)) \
             .save(tmp / f"{f:05d}.png")

    subprocess.run(
        f"ffmpeg -y -framerate {FPS} -i {tmp}/%05d.png -c:v libx264 -crf 28 "
        f"-preset veryfast -pix_fmt yuv420p {out}",
        shell=True, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    out = Path(__file__).parent.parent / "assets" / "overlays"
    out.mkdir(parents=True, exist_ok=True)
    # варианты плотности — робот выбирает один на ролик
    for i, cnt in enumerate(SPARK_COUNTS, 1):
        make_sparks(out / f"sparks_{i}.mp4", seed=i, count=cnt)
        print("sparks", i)
