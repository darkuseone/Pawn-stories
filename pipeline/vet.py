"""
vet.py — автоматическая отбраковка материала. Заменяет человека на шаге отбора.

    python pipeline/vet.py jobs/pawn-02.json

Раньше между скачиванием и монтажом стоял человек: смотрел контактные листы и
называл номера негодного. Это работало, но означало ручной шаг в каждом ролике,
а в подборку регулярно попадают бананы по запросу «свет лампы на старом дереве»
и эскалаторы торгового центра по запросу «блошиный рынок».

Здесь тот же отбор делает робот, в два прохода:

  1. ДЕШЁВЫЙ, локальный. Ничего не стоит и ловит брак формы: почти белый кадр
     (каталожная съёмка на белом фоне — на тёплом тёмном цветокоре канала это
     бледный прямоугольник), проваленную темень, мелкое разрешение, и главное —
     статичное «видео», где за десять секунд не меняется ничего.

  2. ЗРЕНИЕ, через xAI. Модели показывается кадр и рассказывается, о чём ролик.
     Она отвечает, годится ли кадр и почему. Это единственный способ отличить
     монету от петуха: ни теги стока, ни имя файла этого не знают.

Второй проход НЕОБЯЗАТЕЛЕН. Нет ключа, модель недоступна, сеть легла — работает
только первый, в лог уходит внятное предупреждение, сборка не останавливается.
Проверка, которая роняет работающий пайплайн, хуже отсутствующей.

Результат ложится в work/<id>/assets/vetted.json и читается монтажом наравне с
ручным reject из спецификации. Файлы НЕ удаляются: вердикт отменяется правкой
одного поля, без повторного скачивания.
"""

import base64
import io
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

XAI = "https://api.x.ai/v1"
# Модель зрения. Меняется полем vet_model в спецификации — имена моделей
# живут своей жизнью и переживают этот код.
DEFAULT_VET_MODEL = "grok-2-vision-1212"

PROBE_W = 512          # кадр под проверку: больше модели не нужно
WORKERS = 6            # запросов к зрению одновременно
VET_TIMEOUT = 60

# Пороги дешёвого прохода. Подобраны по разбору первого прогона pawn-01.
PALE_LIMIT = 0.55      # доля почти белого, выше которой кадр — каталог на белом
DARK_MEAN = 16         # средняя яркость, ниже которой кадр просто чёрный
MIN_PIXELS = 640 * 360
STATIC_DELTA = 1.6     # средняя разница кадров видео, ниже — стоп-кадр


def log(*a):
    print(*a, flush=True)


# ─────────────────────── ДЕШЁВЫЙ ПРОХОД ───────────────────────

def video_frames(path: Path, n=3):
    """n кадров, равномерно по клипу. Первые кадры у стоков часто чёрные."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        dur = 0.0
    if dur <= 0:
        return []
    out = []
    for k in range(1, n + 1):
        at = dur * k / (n + 1)
        tmp = path.with_suffix(f".probe{k}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}",
                        "-i", str(path), "-frames:v", "1", "-y", str(tmp)],
                       check=False)
        if tmp.exists():
            try:
                out.append(Image.open(tmp).convert("RGB").copy())
            except Exception:
                pass
            tmp.unlink(missing_ok=True)
    return out


def cheap_problems(path: Path):
    """
    Брак формы, который виден без всякого зрения. Возвращает (кадр, список бед).
    Кадр отдаётся наружу, чтобы не декодировать файл второй раз ради модели.
    """
    bad = []
    if path.suffix.lower() in (".mp4", ".m4v"):
        frames = video_frames(path)
        if not frames:
            return None, ["файл не открылся"]
        im = frames[len(frames) // 2]
        # Статичное «видео». Сток иногда отдаёт кадр, растянутый на десять
        # секунд: формально это видео, на экране — фотография без движения,
        # и вся идея перебивки на нём ломается.
        if len(frames) >= 2:
            import numpy as np
            a = np.asarray(frames[0].resize((160, 90)), float)
            b = np.asarray(frames[-1].resize((160, 90)), float)
            delta = abs(a - b).mean()
            if delta < STATIC_DELTA:
                bad.append(f"видео без движения (разница кадров {delta:.2f})")
    else:
        try:
            im = Image.open(path).convert("RGB")
        except Exception as e:
            return None, [f"файл не открылся: {e}"]

    w, h = im.size
    if w * h < MIN_PIXELS:
        bad.append(f"мелкое разрешение {w}x{h}")

    small = im.convert("L").resize((64, 36))
    px = list(small.getdata())
    pale = sum(1 for p in px if p > 224) / len(px)
    mean = sum(px) / len(px)
    if pale > PALE_LIMIT:
        bad.append(f"почти белый кадр ({pale*100:.0f}% площади)")
    if mean < DARK_MEAN:
        bad.append(f"кадр практически чёрный (яркость {mean:.0f})")

    return im, bad


# ─────────────────────── ЗРЕНИЕ ───────────────────────

PROMPT = """You are selecting stock material for a documentary-style YouTube video.

THE VIDEO IS ABOUT: {topic}
{description}

Look at the attached frame and decide whether it can be used as illustrative \
footage in this video.

ACCEPT the frame if it shows: the subject matter itself, related objects, \
period or antique items, hands handling or examining objects, workshop or \
shop or market or auction interiors, archival photographs or paintings, \
textures such as aged wood, metal, paper, fabric, or any atmospheric shot \
that a viewer would accept as belonging to this video.

REJECT the frame if it shows: something clearly unrelated to the subject \
(food, animals, sports, modern offices, shopping malls, vehicles, nature \
landscapes, people in modern clothing in modern settings, fireworks, \
cosmetics, abstract computer graphics, medical or laboratory imagery), \
or if it is a chart, a plain book cover with no imagery, a screenshot, a \
watermark or logo card, or an architectural survey photo of a building \
exterior.

Answer with STRICT JSON and nothing else:
{{"keep": true or false, "why": "at most 12 words", "what": "what you see, at most 8 words"}}"""


def to_data_url(im: Image.Image) -> str:
    im = im.copy()
    im.thumbnail((PROBE_W, PROBE_W))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=78)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def ask_vision(im: Image.Image, topic: str, description: str, model: str,
               key: str, tries=2):
    """
    Вердикт модели по одному кадру. При любой беде возвращает None —
    «не знаю», и кадр остаётся в работе. Отбраковывать по неудавшемуся
    запросу нельзя: так молча пропадёт весь материал при первом же сбое сети.
    """
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(
                topic=topic, description=description)},
            {"type": "image_url",
             "image_url": {"url": to_data_url(im), "detail": "low"}},
        ]}],
    }
    for attempt in range(tries):
        try:
            r = requests.post(f"{XAI}/chat/completions", timeout=VET_TIMEOUT,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=body)
            if r.status_code != 200:
                if attempt + 1 < tries and r.status_code in (429, 500, 502, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                return None, f"зрение ответило {r.status_code}: {r.text[:120]}"
            txt = r.json()["choices"][0]["message"]["content"].strip()
            # модель иногда оборачивает JSON в ```json ... ```
            if txt.startswith("```"):
                txt = txt.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            return bool(data.get("keep", True)), str(
                data.get("what") or data.get("why") or "")[:70]
        except Exception as e:
            if attempt + 1 < tries:
                time.sleep(1.5)
                continue
            return None, f"зрение не ответило: {e}"
    return None, "зрение не ответило"


# ─────────────────────── ГЛАВНОЕ ───────────────────────

def index_of(path: Path) -> int:
    return int(path.name.split("_")[1])


def topic_text(job):
    t = job.get("topic") or {}
    y = job.get("youtube") or {}
    topic = t.get("slug", "").replace("-", " ") or y.get("title", "")
    kw = ", ".join(t.get("keywords", []))
    desc = y.get("description_intro", "")
    return (topic + (f" ({kw})" if kw else ""),
            f"More context: {desc}" if desc else "")


def vet_all(job, work: Path, use_vision=True):
    topic, desc = topic_text(job)
    model = job.get("vet_model", DEFAULT_VET_MODEL)
    key = (os.environ.get("XAI_API_KEY") or "").strip()

    groups = {"clip": sorted((work / "footage").glob("clip_*")),
              "arch": sorted((work / "archive").glob("arch_*"))}

    verdicts = {"clip": {}, "arch": {}}
    vision_ok = use_vision and bool(key)
    if use_vision and not key:
        log("  ! нет XAI_API_KEY — зрение выключено, останется дешёвый проход")

    for kind, files in groups.items():
        if not files:
            continue
        log(f"── проверяю {kind}: {len(files)} шт")

        # первый проход: дёшево и локально
        frames, cheap_out = {}, {}
        for f in files:
            im, bad = cheap_problems(f)
            frames[f] = im
            cheap_out[f] = bad

        # второй проход: зрение, только для тех, кто пережил первый
        survivors = [f for f in files if not cheap_out[f] and frames[f]]
        vision_out = {}
        if vision_ok and survivors:
            def one(f):
                return f, ask_vision(frames[f], topic, desc, model, key)
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for f, res in ex.map(one, survivors):
                    vision_out[f] = res
            unknown = sum(1 for v in vision_out.values() if v[0] is None)
            if unknown == len(survivors):
                log(f"  ! зрение не ответило ни разу "
                    f"({list(vision_out.values())[0][1]}) — "
                    f"оставляю всё, что прошло дешёвый проход")

        kept = 0
        for f in files:
            n = index_of(f)
            if cheap_out[f]:
                verdicts[kind][str(n)] = {"keep": False,
                                          "why": "; ".join(cheap_out[f])}
                continue
            keep, why = vision_out.get(f, (None, "не проверялось зрением"))
            if keep is None:
                keep = True                 # непонятный ответ — не отбраковка
            verdicts[kind][str(n)] = {"keep": bool(keep), "why": why}
            kept += bool(keep)

        dropped = len(files) - kept
        log(f"   годных {kept}, отбраковано {dropped}")
        for n, v in sorted(verdicts[kind].items(), key=lambda x: int(x[0])):
            if not v["keep"]:
                log(f"     {kind} {int(n):03d}: {v['why']}")

    out = work / "vetted.json"
    out.write_text(json.dumps(verdicts, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    log(f"── вердикты записаны в {out}")
    return verdicts


def rejected_from(work: Path):
    """Номера, забракованные роботом. Читается монтажом."""
    p = work / "vetted.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {kind: sorted(int(n) for n, v in d.items() if not v.get("keep"))
            for kind, d in data.items()}


def main(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    work = Path("work") / job["id"] / "assets"
    if not work.exists():
        raise SystemExit(f"нет {work} — сначала собери материал")
    vet_all(job, work, use_vision=job.get("vet_vision", True))


if __name__ == "__main__":
    main(sys.argv[1])
