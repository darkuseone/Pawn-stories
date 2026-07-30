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

# Модель зрения НЕ ЗАШИТА. Первый боевой прогон упёрся в
# «Model not found: grok-2-vision-1212»: имя, взятое из головы, оказалось
# несуществующим, зрение не сработало ни разу, и понятно это стало только
# из лога. Имена моделей живут своей жизнью и переживают любой код, который
# их угадывает.
#
# Поэтому модель ВЫБИРАЕТСЯ НА МЕСТЕ: список берётся у самого сервиса через
# /v1/models, кандидаты сортируются по предпочтению, и первый, который
# реально ответил на пробную картинку, идёт в работу. Поле vet_model в
# спецификации перебивает выбор, если нужно закрепить конкретную.
# Порядок предпочтения. Сначала явно НЕ рассуждающие: для ответа «годится
# или нет» рассуждение бесполезно, а платится оно выходными токенами.
# Дальше по возрастанию цены.
MODEL_PREFERENCE = ("non-reasoning", "nonreasoning", "non_reasoning",
                    "build", "vision", "4.20", "4-20", "4.3", "4-3",
                    "grok-4", "grok-3", "grok-2")
# Заведомо не для зрения: генераторы картинок и эмбеддинги.
MODEL_EXCLUDE = ("image", "imagine", "embed", "tts", "whisper")

PROBE_W = 512          # кадр под проверку: больше модели не нужно
WORKERS = 6            # запросов к зрению одновременно
VET_TIMEOUT = 60

# Цена зрения, долларов за миллион токенов. Сверено с прайс-листом xAI
# (скриншоты консоли, июль 2026). Ключ — кусок имени модели, значение —
# (вход, выход). Берётся первая подошедшая строка, иначе DEFAULT.
#
# Считать надо по ТОЙ модели, которую выбрал choose_model, а не по одной
# зашитой цифре: между самой дешёвой и самой дорогой из тех, что умеют
# зрение, разница втрое по выходу.
PRICE_TABLE = (
    ("build",   (1.00, 2.00)),   # Grok Build 0.1 — самый дешёвый со зрением
    ("4.20",    (1.25, 2.50)),   # Grok 4.20, в том числе Non-Reasoning
    ("4-20",    (1.25, 2.50)),
    ("4.3",     (1.25, 2.50)),   # Grok 4.3
    ("4-3",     (1.25, 2.50)),
    ("4.5",     (2.00, 6.00)),   # Grok 4.5 — дороже всех, выход втрое
    ("4-5",     (2.00, 6.00)),
)
PRICE_DEFAULT = (1.25, 2.50)


def price_of(model: str):
    """(доллары за млн входных, за млн выходных) для выбранной модели."""
    low = (model or "").lower()
    for key, pair in PRICE_TABLE:
        if key in low:
            return pair
    return PRICE_DEFAULT


# ГЕНЕРАЦИЯ ВИДЕО НЕ ИСПОЛЬЗУЕТСЯ, и вот почему в цифрах. Grok Imagine
# Video 1.5 стоит $0.25 за секунду в 1080p, то есть трёхсекундная перебивка
# — 75 центов. Одна сгенерированная картинка стоит 2-5 центов, а проверка
# кадра зрением — меньше цента. Нехватку футажа закрывает фотография с
# движением камеры: на общем плане её от плавного стокового кадра не
# отличить, а стоит она в тридцать раз дешевле.

# Источники, которым верим без зрения. Замер по первому прогону pawn-01,
# 40 архивных фото: Met Museum дал почти весь годный материал — это музей
# предметов, и по предметному запросу он отдаёт предметы. Library of
# Congress по тем же запросам отдавал обложки книг и обмеры зданий, поэтому
# в этот список он не входит и проверяется зрением.
TRUSTED_SOURCES = ("met",)

# Пороги дешёвого прохода. Подобраны по разбору первого прогона pawn-01.
#
# У белизны ДВА порога, и это главное в двухъярусной схеме. Выше HARD —
# кадр отбраковывается на месте, бесплатно. Между SOFT и HARD — случай
# непонятный: каталожное фото предмета на светлом фоне бывает и лучшим, что
# есть по теме. Такие уходят к зрению, а не в мусор.
PALE_HARD = 0.62       # столько белого — это каталожная карточка, а не кадр
PALE_SOFT = 0.38       # отсюда начинается «непонятно», решает зрение
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
            return None, ["файл не открылся"], 0.0
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
            return None, [f"файл не открылся: {e}"], 0.0

    w, h = im.size
    if w * h < MIN_PIXELS:
        bad.append(f"мелкое разрешение {w}x{h}")

    small = im.convert("L").resize((64, 36))
    try:
        px = list(small.get_flattened_data())
    except AttributeError:
        px = list(small.getdata())
    pale = sum(1 for p in px if p > 224) / len(px)
    mean = sum(px) / len(px)
    if pale > PALE_HARD:
        bad.append(f"почти белый кадр ({pale*100:.0f}% площади)")
    if mean < DARK_MEAN:
        bad.append(f"кадр практически чёрный (яркость {mean:.0f})")

    return im, bad, pale


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


def list_models(key):
    """Что сервис вообще умеет. Пустой список — не беда, будет запасной путь."""
    try:
        r = requests.get(f"{XAI}/models", timeout=30,
                         headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            log(f"  ? список моделей не отдался ({r.status_code})")
            return []
        return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:
        log(f"  ? список моделей не отдался ({e})")
        return []


def candidates(key, want=None):
    """Кандидаты в модель зрения, в порядке предпочтения."""
    if want:
        return [want]
    ids = [i for i in list_models(key)
           if not any(x in i.lower() for x in MODEL_EXCLUDE)]
    out = []
    for pat in MODEL_PREFERENCE:
        for i in ids:
            if pat in i.lower() and i not in out:
                out.append(i)
    for i in ids:                      # всё остальное — следом
        if i not in out:
            out.append(i)
    return out


def choose_model(im, topic, desc, key, want=None):
    """
    Подбирает рабочую модель на ОДНОЙ пробной картинке.

    Проверка боем, а не по имени: модель может существовать в списке и не
    принимать изображения. Единственный надёжный признак — она ответила.
    Стоит это один запрос на весь ролик.
    """
    cands = candidates(key, want)
    if not cands:
        return None, "сервис не отдал ни одной модели"
    for name in cands[:6]:
        keep, why, used = ask_vision(im, topic, desc, name, key, tries=1)
        if keep is not None:
            log(f"  зрение: работает модель {name}")
            return name, ""
        log(f"  ? {name} не подошла: {why}")
    return None, f"ни одна из {len(cands[:6])} моделей не приняла картинку"


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
                return None, f"зрение ответило {r.status_code}: {r.text[:120]}", (0, 0)
            txt = r.json()["choices"][0]["message"]["content"].strip()
            # модель иногда оборачивает JSON в ```json ... ```
            if txt.startswith("```"):
                txt = txt.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            u = r.json().get("usage") or {}
            used = (int(u.get("prompt_tokens") or 0),
                    int(u.get("completion_tokens") or 0))
            return (bool(data.get("keep", True)),
                    str(data.get("what") or data.get("why") or "")[:70],
                    used)
        except Exception as e:
            if attempt + 1 < tries:
                time.sleep(1.5)
                continue
            return None, f"зрение не ответило: {e}"
    return None, "зрение не ответило", (0, 0)


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


def manifest_meta(work: Path):
    """Источник и запрос каждого файла — из манифестов скачивания."""
    meta = {}
    for folder in ("footage", "archive"):
        man = work / folder / "_manifest.json"
        if not man.exists():
            continue
        try:
            rows = json.loads(man.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for row in rows:
            name = Path(row.get("file", "")).name
            if name:
                meta[name] = (row.get("src", ""), row.get("q", ""))
    return meta


def words(text: str):
    import re as _re
    stop = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
            "with", "from", "closeup", "close", "historic", "old"}
    return {w for w in _re.findall(r"[a-zA-Z]+", (text or "").lower())
            if len(w) > 2 and w not in stop}


def triage(path: Path, im, bad, pale, src, query, current_queries, trusted):
    """
    ПЕРВЫЙ ЯРУС. Решает бесплатно всё, что можно решить бесплатно, и
    честно говорит «не знаю» там, где нужен взгляд.

    Возвращает "reject" | "keep" | "ask" и причину.

    Смысл ярусов в деньгах. Зрение стоит за каждый кадр, и платить за
    очевидное незачем: белый прямоугольник, чёрный кадр и стоп-кадр вместо
    видео видно арифметикой. Ровно так же незачем платить за то, что и так
    заведомо по теме: музейный предмет из Met по предметному запросу и
    собственная генерация по собственному промпту.

    Зрению достаётся середина — то, про что локальные признаки молчат.
    Именно там и живут бананы по запросу про лампу.
    """
    if bad:
        return "reject", "; ".join(bad)

    # собственная генерация: она нарисована по нашему же промпту
    if path.name.startswith("img_"):
        return "keep", "сгенерировано по промпту ролика"

    # Запрос, которого в спецификации больше нет, — это материал, оставшийся
    # в кэше от прошлой версии ролика. Он не обязательно плох, но и по теме
    # уже не гарантирован, поэтому идёт к зрению, а НЕ в отказ.
    #
    # Отбраковывать здесь по пересечению слов запроса с темой нельзя, и это
    # проверено: запросы пишутся под ролик и заведомо по теме, но написаны
    # они другими словами. «hands examining antique object» не пересекается
    # с ключевыми словами темы ни одним словом — и правило, которое казалось
    # разумным, забраковало на тесте весь годный материал разом.
    if current_queries and query and query not in current_queries:
        return "ask", f"запрос «{query}» не из текущей спецификации"

    # доверенный источник — смотреть незачем
    if src in trusted:
        return "keep", f"{src}: предметный источник, отдаёт предметы"

    if pale >= PALE_SOFT:
        return "ask", f"светлый фон {pale*100:.0f}% — нужен взгляд"
    return "ask", "локальные признаки молчат"


def vet_all(job, work: Path, use_vision=True):
    topic, desc = topic_text(job)
    # Модель не задаётся здесь: её подбирает choose_model ниже, на пробной
    # картинке. Раньше на этом месте стояла зашитая константа с угаданным
    # именем — она и оказалась несуществующей.
    model = None
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    trusted = tuple(job.get("trusted_sources", TRUSTED_SOURCES))
    current_queries = set(job.get("footage_queries", []) +
                          job.get("archive_queries", []))

    meta = manifest_meta(work)
    groups = {"clip": sorted((work / "footage").glob("clip_*")),
              "arch": sorted((work / "archive").glob("arch_*"))}

    verdicts = {"clip": {}, "arch": {}}
    tok_in = tok_out = asked = 0
    vision_ok = use_vision and bool(key)
    if use_vision and not key:
        log("  ! нет XAI_API_KEY — зрение выключено, работает только "
            "локальный ярус")

    # Рабочая модель ищется ОДИН раз на ролик, до всех проверок: иначе
    # неверное имя означает полсотни одинаковых отказов подряд и ноль
    # пользы, ровно как на первом прогоне.
    if vision_ok:
        probe = None
        for files in groups.values():
            for f in files:
                im, bad, _ = cheap_problems(f)
                if im is not None:
                    probe = im
                    break
            if probe is not None:
                break
        if probe is None:
            vision_ok = False
        else:
            model, why = choose_model(probe, topic, desc, key,
                                      job.get("vet_model"))
            if not model:
                log(f"  ! зрение недоступно: {why} — работает только "
                    f"локальный ярус, спорное остаётся в работе")
                vision_ok = False

    for kind, files in groups.items():
        if not files:
            continue
        log(f"── проверяю {kind}: {len(files)} шт")

        decided, ask_list, frames = {}, [], {}
        for f in files:
            im, bad, pale = cheap_problems(f)
            frames[f] = im
            src, query = meta.get(f.name, ("", ""))
            verdict, why = triage(f, im, bad, pale, src, query,
                                  current_queries, trusted)
            if verdict == "ask" and im is not None:
                ask_list.append((f, why))
            else:
                decided[f] = (verdict == "keep", why)

        log(f"   первый ярус решил {len(decided)} бесплатно, "
            f"зрению осталось {len(ask_list)}")

        vision_out = {}
        if vision_ok and ask_list:
            def one(item):
                f, _why = item
                return f, ask_vision(frames[f], topic, desc, model, key)
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for f, res in ex.map(one, ask_list):
                    vision_out[f] = res
                    tok_in += res[2][0]
                    tok_out += res[2][1]
                    asked += 1
            unknown = sum(1 for v in vision_out.values() if v[0] is None)
            if unknown == len(ask_list):
                log(f"  ! зрение не ответило ни разу "
                    f"({list(vision_out.values())[0][1]}) — "
                    f"спорное оставляю в работе")
        elif ask_list:
            log("   зрение недоступно — спорное оставляю в работе")

        kept = 0
        for f in files:
            n = index_of(f)
            if f in decided:
                keep, why = decided[f]
            else:
                keep, why, _ = vision_out.get(
                    f, (True, "зрение не спрашивалось", (0, 0)))
                if keep is None:
                    keep, why = True, f"неясный ответ зрения: {why}"
            verdicts[kind][str(n)] = {"keep": bool(keep), "why": why}
            kept += bool(keep)

        log(f"   годных {kept}, отбраковано {len(files) - kept}")
        for n, v in sorted(verdicts[kind].items(), key=lambda x: int(x[0])):
            if not v["keep"]:
                log(f"     {kind} {int(n):03d}: {v['why']}")

    if asked:
        p_in, p_out = price_of(model or "")
        cost = tok_in / 1e6 * p_in + tok_out / 1e6 * p_out
        log(f"── зрение ({model}): {asked} кадров, "
            f"{tok_in} входных и {tok_out} выходных токенов")
        log(f"   ${cost:.3f} за ролик, ${cost/asked*1000:.2f} за тысячу кадров "
            f"(тариф ${p_in:.2f}/${p_out:.2f} за млн). "
            f"Токены измерены по полю usage в ответах API.")
    elif not vision_ok:
        log("── зрение НЕ РАБОТАЛО: отбор сделан одним локальным ярусом, "
            "спорный материал остался в ролике")
    else:
        log("── зрение не понадобилось: всё решил первый ярус")

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
