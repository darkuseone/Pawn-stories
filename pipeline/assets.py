"""
assets.py — собирает всё, из чего потом монтируется ролик.

Три источника:
  1. ElevenLabs — озвучка блоками, с посимвольными тайм-кодами.
     Тайм-коды нужны, чтобы кадры менялись на границах предложений,
     а не по таймеру. Механическая нарезка через равные промежутки
     видна зрителю сразу.
  2. xAI — генерация изображений. Через batch вдвое дешевле, но до суток.
  3. Шесть открытых архивов — реальные фото и хроника. Только
     общественное достояние и CC0, всё что требует атрибуции отсекается.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job
import vet

UA = {"User-Agent": "sleep-docs-pipeline/1.0 (educational video project)"}
TIMEOUT = 60

# Потолки на скачивание материала. Ролику нужны отрывки на 5-15 секунд,
# и ничего тяжелее сюда не требуется. Без потолков этап 1 однажды провисел
# 37 минут на одном файле с archive.org.
MAX_FILE_BYTES = 120 * 1024 * 1024      # 120 МБ на файл
FETCH_SECONDS = 90                      # столько ждём один файл
GATHER_BUDGET = 420                     # столько всего на один сбор

# ПОТОЛОК НА ДЛИНУ И РАЗРЕШЕНИЕ ФУТАЖА.
#
# Ролику от стокового клипа нужны отрывки по 2-15 секунд, и ClipCutter всё
# равно режет файл на куски. Держать ради этого минутный ролик в 4K — значит
# впустую занимать кэш и упираться в потолок Releases в 2 ГБ: на прошлом
# прогоне кэш материала весил 1.06 ГБ при 91 клипе.
#
# Поэтому: качаем рендер 720p-1080p (не 4K), клипы длиннее 25 секунд
# подрезаем на диске сразу после скачивания. Обрезка идёт БЕЗ
# перекодирования, потоковым копированием — это секунды на файл и никакой
# потери качества.
MAX_CLIP_SECONDS = 25
CLIP_MIN_WIDTH = 1280                   # ниже 720p не берём — заметно на экране
CLIP_MAX_WIDTH = 1920                   # выше 1080p не нужно, только вес


def trim_long_clip(path: Path, limit: float = MAX_CLIP_SECONDS) -> None:
    """Подрезает скачанный клип до потолка. Тихо ничего не делает, если короче."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        return
    if dur <= limit + 0.5:
        return
    tmp = path.with_suffix(".trim.mp4")
    # -c copy режет по ближайшему ключевому кадру: не по-кадрово точно, но
    # для отрывка из середины стока это безразлично, зато мгновенно.
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-t", f"{limit:.2f}",
         "-c", "copy", "-an", str(tmp)], capture_output=True)
    if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 20000:
        was = path.stat().st_size // 1048576
        tmp.replace(path)
        log(f"    подрезан с {dur:.0f} до {limit:.0f} с "
            f"({was} -> {path.stat().st_size // 1048576} МБ)")
    else:
        tmp.unlink(missing_ok=True)


def log(*a):
    print(*a, flush=True)


# ────────────────────────── ОЗВУЧКА ──────────────────────────

def tts_block(text, out_mp3: Path, voice_id, api_key, stability=0.42,
              similarity=0.78, style=0.10):
    """
    Один блок текста → mp3 + выравнивание по символам.
    Настройки голоса чуть плавают от ролика к ролику — иначе интонация
    становится одинаковой на всём канале.
    """
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
           f"/with-timestamps")
    r = requests.post(url, timeout=TIMEOUT,
                      headers={"xi-api-key": api_key,
                               "Content-Type": "application/json"},
                      json={"text": text,
                            "model_id": "eleven_multilingual_v2",
                            "voice_settings": {
                                "stability": stability,
                                "similarity_boost": similarity,
                                "style": style,
                                "use_speaker_boost": True}})
    if r.status_code != 200:
        # ElevenLabs пишет причину в тело ответа: истёкший ключ, исчерпанная
        # квота, чужой voice_id, блокировка облачного IP на бесплатном тарифе.
        # raise_for_status тело выбрасывает, и в логе остаётся голое
        # «401 Unauthorized» без единой подсказки, что чинить.
        msg = f"ElevenLabs {r.status_code}: {r.text[:500]}"
        # Самая частая причина — не тот voice_id: он у каждого аккаунта свой,
        # и чужой идентификатор из чужой спецификации не работает. Гадать по
        # коду ответа не нужно, список голосов аккаунта отдаётся тем же ключом.
        if "voice" in r.text.lower() or r.status_code in (400, 404):
            msg += "\n" + available_voices(api_key)
        raise RuntimeError(msg)
    data = r.json()
    import base64
    out_mp3.write_bytes(base64.b64decode(data["audio_base64"]))
    al = data.get("alignment") or data.get("normalized_alignment") or {}
    return {
        "chars": al.get("characters", []),
        "starts": al.get("character_start_times_seconds", []),
        "ends": al.get("character_end_times_seconds", []),
    }


def available_voices(api_key, limit=25):
    """
    Список голосов аккаунта — для сообщения об ошибке.

    voice_id у каждого аккаунта свой: идентификатор, скопированный из чужой
    спецификации или из статьи, не работает. Без этого списка отказ выглядит
    как «422 Unprocessable Entity» и не подсказывает ничего.

    Сама по себе никогда не роняет прогон: это диагностика, а не проверка.
    """
    try:
        r = requests.get("https://api.elevenlabs.io/v1/voices", timeout=30,
                         headers={"xi-api-key": api_key})
        if r.status_code != 200:
            return f"(список голосов получить не вышло: {r.status_code})"
        rows = [f"  {v.get('voice_id')}  {v.get('name')}"
                for v in r.json().get("voices", [])[:limit]]
        if not rows:
            return "(в аккаунте нет ни одного голоса)"
        return ("Голоса этого аккаунта — впиши нужный в поле voice_id "
                "спецификации:\n" + "\n".join(rows))
    except Exception as e:
        return f"(список голосов получить не вышло: {e})"


def sentence_marks(text, align, offset):
    """
    Превращает посимвольные тайм-коды в границы предложений.
    Это и есть точки, где робот будет менять кадр.
    """
    chars, starts, ends = align["chars"], align["starts"], align["ends"]
    if not chars:
        return []
    marks, buf, buf_start = [], [], None
    for i, ch in enumerate(chars):
        if buf_start is None:
            buf_start = starts[i]
        buf.append(ch)
        if ch in ".!?" and i + 1 < len(chars) and chars[i + 1] in " \n":
            marks.append({"text": "".join(buf).strip(),
                          "start": round(buf_start + offset, 3),
                          "end": round(ends[i] + offset, 3)})
            buf, buf_start = [], None
    if buf:
        marks.append({"text": "".join(buf).strip(),
                      "start": round((buf_start or 0) + offset, 3),
                      "end": round(ends[-1] + offset, 3)})
    return marks


def build_voice(job, work: Path):
    # strip обязателен: при вставке в Settings к значению легко цепляется
    # перенос строки или пробел, и API отвечает 401 без объяснений
    key = os.environ["ELEVENLABS_API_KEY"].strip()
    # Голос принадлежит КАНАЛУ, а не репозиторию. Читается из спецификации
    # ролика; секрет остаётся запасным путём, чтобы старые спецификации без
    # поля voice_id продолжали работать.
    # Голос берётся из секрета ELEVENLABS_VOICE_ID: он один на канал и
    # лежит там же, где ключи. Поле voice_id в спецификации осталось
    # переопределением на случай, когда конкретному ролику нужен другой
    # голос, но пустым оно теперь НЕ значит «нет голоса».
    voice = (os.environ.get("ELEVENLABS_VOICE_ID", "")
             or job.get("voice_id", "")).strip()
    if not voice:
        raise SystemExit(
            "не задан голос: секрет ELEVENLABS_VOICE_ID в Settings -> "
            "Secrets and variables -> Actions, либо поле voice_id в "
            "спецификации ролика")
    vs = job.get("voice_settings", {})
    adir = work / "voice"
    adir.mkdir(parents=True, exist_ok=True)

    parts, marks, offset = [], [], 0.0
    for i, block in enumerate(job["script_blocks"], 1):
        mp3 = adir / f"block_{i:02d}.mp3"
        # тайм-коды нужны наравне с mp3: если из кэша приехал только звук,
        # блок переозвучивается, иначе ниже падение на чтении json
        if mp3.exists() and (adir / f"block_{i:02d}.json").exists():
            log(f"  блок {i} уже озвучен, пропускаю")
        else:
            log(f"  озвучиваю блок {i} ({len(block)} символов)")
            al = tts_block(block, mp3, voice, key,
                           vs.get("stability", 0.42),
                           vs.get("similarity", 0.78),
                           vs.get("style", 0.10))
            (adir / f"block_{i:02d}.json").write_text(json.dumps(al))
        al = json.loads((adir / f"block_{i:02d}.json").read_text())
        marks += sentence_marks(block, al, offset)
        dur = _duration(mp3)
        offset += dur
        parts.append(mp3)

    # склейка блоков в одну дорожку
    full = work / "voice_full.m4a"
    lst = adir / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    # было os.system с заглушенным выводом: ошибка склейки терялась, дорожки
    # не появлялось, и падал уже монтаж — на другом шаге и без причины
    import subprocess
    subprocess.run(f'ffmpeg -y -f concat -safe 0 -i "{lst}" -c:a aac -b:a 192k '
                   f'"{full}"', shell=True, check=True,
                   stdout=subprocess.DEVNULL)
    (work / "marks.json").write_text(json.dumps(marks, indent=1))
    log(f"  озвучка готова: {offset:.1f} сек, {len(marks)} предложений")
    return full, marks, offset


def _duration(p: Path):
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


# ────────────────────────── ИЗОБРАЖЕНИЯ ──────────────────────────

XAI = "https://api.x.ai/v1"

# ПОДТВЕРЖДЕНО ДОКУМЕНТАЦИЕЙ (docs.magnific.com): базовый URL, заголовок
# авторизации и асинхронная модель — POST отдаёт task_id и status
# IN_PROGRESS, готовый результат забирается GET на тот же путь + /{task_id}.
# webhook_url в запросе не используется: ролик собирается одноразовым
# скриптом в GitHub Actions, у которого нет публичного адреса принять
# обратный вызов — см. обсуждение с каналом. Поэтому везде поллинг.
MAGNIFIC_API = "https://api.magnific.com"
MAGNIFIC_AUTH_HEADER = "x-magnific-api-key"

# Условия оговорены с каналом, не угаданы. 70% генерации картинок уходит на
# Magnific (безлимит по подписке), 30% остаётся на xAI — ровно то деление,
# что было раньше на 100% xAI, только теперь основной объём платит подписка,
# а не поштучная генерация.
#
# Видео Magnific генерирует ТОЛЬКО короткими вставками (2-3 с) и ТОЛЬКО туда,
# где реального футажа по теме не нашлось вообще ни на одном стоке и архиве
# — не больше 5% клипов ролика. 60-70% экранного времени остаются реальным
# материалом, как было оговорено раньше; это не новая договорённость, а то
# же самое число, просто теперь под него явно заведён потолок в коде.
MAGNIFIC_IMAGE_SHARE = 0.70
MAGNIFIC_VIDEO_GEN_SHARE = 0.05
MAGNIFIC_STOCK_DAILY_CAP = 15           # сток Magnific: видео+фото вместе, в сутки

# Подтверждено логом ff-ep06: API Mystic отверг flux2pro / nano-banana2 /
# seedream5pro (400; valid: fluid, realism, zen, flexible, super_real,
# editorial…). Крутим три рабочих имени; переопределение —
# job["magnific_image_models"].
MAGNIFIC_IMAGE_MODELS = ["realism", "fluid", "zen"]

# Kling и MiniMax Hailuo УБРАНЫ по решению канала — не использовать, хотя
# подписка их и даёт. Остались Seedance и WAN.
#
# НЕПОДТВЕРЖДЕНО ЕЩЁ СИЛЬНЕЕ, чем модели картинок: документация вообще не
# даёт путей для видео (только имена провайдеров в таблице каталога).
# Путь /v1/ai/<engine> — это перенос схемы Mystic по аналогии, а не факт
# из документации. Смотри предупреждение в
# fill_missing_footage_via_magnific — эта часть не должна уйти в платный
# прогон непроверенной.
MAGNIFIC_VIDEO_MODELS = ["seedance-1.5pro", "wan-2.2"]


def _magnific_headers(key):
    return {MAGNIFIC_AUTH_HEADER: key, "Content-Type": "application/json"}


def _magnific_poll(path: str, task_id: str, key, timeout=300):
    """
    Поллинг асинхронной задачи Magnific — ПОДТВЕРЖДЕНО документацией
    (раздел 3): GET на тот же путь + /{task_id}, статусы IN_PROGRESS ->
    COMPLETED/FAILED, результат в ответе под ключом "data".

    Экспоненциальная задержка 1-2-4-8...с потолком 15 с — так рекомендует
    сама документация канала, а не выдумано: часть задач Mystic закрывается
    за секунды, часть (видео) — за минуты, и опрашивать обе на одном
    фиксированном интервале значит либо долбить API зря на быстрых, либо
    ждать лишний круг на медленных.
    """
    deadline = time.time() + timeout
    delay = 1.0
    while time.time() < deadline:
        r = requests.get(f"{MAGNIFIC_API}{path}/{task_id}", timeout=TIMEOUT,
                         headers=_magnific_headers(key))
        if r.status_code != 200:
            raise RuntimeError(f"magnific: опрос {task_id} — "
                              f"{r.status_code} {r.text[:200]}")
        data = r.json().get("data", {})
        status = data.get("status")
        if status == "COMPLETED":
            return data
        if status == "FAILED":
            raise RuntimeError(f"magnific: задача {task_id} провалилась — "
                              f"{json.dumps(data)[:200]}")
        time.sleep(delay)
        delay = min(delay * 2, 15.0)
    raise TimeoutError(f"magnific: задача {task_id} не завершилась за "
                       f"{timeout} с")


def split_indexed(prompts, share):
    """
    Делит промпты между двумя провайдерами по доле, блоками по десять.

    Ровно 70/30 подряд (сначала все magnific, потом весь xAI) означало бы,
    что первые две трети ролика несут один почерк генерации, а последняя
    треть — другой, и разница будет видна на стыке. Блок в десять достаточно
    мелкий, чтобы почерк перемешался по всему ролику, и достаточно крупный,
    чтобы не дробить запросы к провайдеру по одной картинке за раз.

    Индексы 1-based и совпадают с позицией в job["image_prompts"]: build.py
    ищет картинку по номеру img_NNN, привязанному к месту в сценарии, а не
    по порядку в этом вызове.
    """
    cut = round(share * 10)
    a, b = [], []
    for i, p in enumerate(prompts):
        (a if i % 10 < cut else b).append((i + 1, p))
    return a, b


def images_sync(indexed_prompts, out: Path, model, key):
    """
    Быстрый режим: по одному запросу, полная цена, готово за минуты.

    indexed_prompts — пары (номер_в_сценарии, промпт), а не голый список:
    вызывающий может прислать не весь сценарий разом, а часть (см. деление
    с Magnific в main()), и номер файла обязан остаться номером ИЗ
    СЦЕНАРИЯ, иначе build.py потеряет связь картинки с местом в таймлайне.
    """
    out.mkdir(parents=True, exist_ok=True)
    for idx, p in indexed_prompts:
        dst = out / f"img_{idx:03d}.jpg"
        if dst.exists():
            continue
        r = requests.post(f"{XAI}/images/generations", timeout=180,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model, "prompt": p, "n": 1})
        if r.status_code != 200:
            log(f"  ! картинка {idx} не вышла: {r.status_code} {r.text[:160]}")
            continue
        url = r.json()["data"][0]["url"]
        dst.write_bytes(requests.get(url, timeout=120).content)
        log(f"  картинка {idx} (xai)")


def _magnific_generated_url(data: dict):
    """
    Достаёт ссылку на файл из завершённой задачи Magnific.

    НЕПОДТВЕРЖДЕНО. Документация показывает только `result["generated"]`
    в примере (см. section 5 инструкции канала) без разбора структуры —
    неизвестно, это строка-URL, список строк или список объектов с полем
    url/image. Разобраны все три формы, чтобы не упасть молча на первом же
    расхождении, но сверить с реальным ответом всё равно нужно.
    """
    gen = data.get("generated")
    if isinstance(gen, str):
        return gen
    if isinstance(gen, list) and gen:
        first = gen[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("image") or first.get("image_url")
    if isinstance(gen, dict):
        return gen.get("url") or gen.get("image")
    return None


def images_magnific(indexed_prompts, out: Path, key):
    """
    Основной объём картинок ролика (70% по умолчанию) — здесь, через
    подписку без лимита на генерацию.

    Эндпойнт /v1/ai/mystic и асинхронная пара POST+GET(task_id) —
    ПОДТВЕРЖДЕНО документацией канала (Mystic — «фирменный движок»,
    рекомендованный там же). Имена в MAGNIFIC_IMAGE_MODELS — realism /
    fluid / zen: подтверждены ответом API на ff-ep06 (старые ярлыки
    канала отвергались с 400).

    Три модели по кругу, а не одна: одна модель на весь объём дала бы
    ролику однородный почерк генерации, от которого и так уводит вектор
    стиля в variation.py на монтаже.

    Без webhook_url: ролик собирается одноразовым скриптом в GitHub
    Actions, отвечать на обратный вызов некому — поэтому поллинг
    (_magnific_poll), как и советует документация в этом случае.
    """
    out.mkdir(parents=True, exist_ok=True)
    for i, (idx, p) in enumerate(indexed_prompts):
        dst = out / f"img_{idx:03d}.jpg"
        if dst.exists():
            continue
        model = MAGNIFIC_IMAGE_MODELS[i % len(MAGNIFIC_IMAGE_MODELS)]
        r = requests.post(f"{MAGNIFIC_API}/v1/ai/mystic", timeout=TIMEOUT,
                          headers=_magnific_headers(key),
                          json={"prompt": p, "model": model})
        if r.status_code != 200:
            log(f"  ! magnific картинка {idx} ({model}) не запустилась: "
                f"{r.status_code} {r.text[:160]}")
            continue
        task_id = (r.json().get("data") or {}).get("task_id")
        if not task_id:
            log(f"  ! magnific картинка {idx}: в ответе нет task_id")
            continue
        try:
            data = _magnific_poll("/v1/ai/mystic", task_id, key)
        except (RuntimeError, TimeoutError) as e:
            log(f"  ! magnific картинка {idx} ({model}): {e}")
            continue
        url = _magnific_generated_url(data)
        if not url:
            log(f"  ! magnific картинка {idx}: в ответе нет ссылки "
                f"({json.dumps(data)[:160]})")
            continue
        dst.write_bytes(requests.get(url, timeout=120).content)
        log(f"  картинка {idx} (magnific/{model})")


class BatchFailed(RuntimeError):
    """
    Пакет не отдал ни одной картинки.

    Отдельный тип исключения нужен, чтобы вызывающий мог отличить «пакет
    не сложился, пробуй поштучно» от настоящей поломки сети или ключа и
    не глотать вторую под видом первой.

    alive различает два принципиально разных случая, и путать их дорого:

      alive=False  пакет мёртв (провалился, протух, потерял файл
                   результатов). Ждать нечего, состояние сброшено, и
                   поштучная догенерация — единственный способ спасти
                   уже оплаченную озвучку.

      alive=True   пакет ЖИВ, просто долгий. Состояние сохранено. Уходить
                   здесь в поштучную генерацию нельзя: пакет всё равно
                   досчитается и всё равно будет выставлен в счёт, и мы
                   заплатим за одни и те же картинки дважды — сначала
                   половинную цену за пакет, потом полную за поштучные.
    """

    def __init__(self, message, alive=False):
        super().__init__(message)
        self.alive = alive


def images_batch(indexed_prompts, out: Path, model, key, poll=120, max_wait=5400):
    """
    Дешёвый режим: пакет заданий, минус 50% от цены, до суток ожидания.
    Ссылки на готовые файлы живут около часа, поэтому качаем сразу
    как только пакет закрылся.

    ПУСТОЙ ПАКЕТ — ЭТО ОШИБКА, А НЕ РЕЗУЛЬТАТ. Так этот код и подвёл на
    прогоне ff-ep05: пакет ушёл, вернулся без единой картинки, функция
    написала «скачано 0 картинок» и вернула управление как ни в чём не
    бывало. Дальше отработали отбраковка и добор материала, пакет
    уехал в кэш, и падение случилось только на монтаже — через десять
    минут и уже после того, как за озвучку заплачено.

    Отдельная беда была в том, что ноль картинок НЕ СБРАСЫВАЛ состояние:
    `_batch.json` с мёртвым идентификатором уезжал в кэш, и каждый
    следующий запуск доставал его оттуда, опрашивал давно закрытый пакет
    и снова получал ноль. Ролик залипал намертво, и починить это можно
    было только руками, вычистив кэш.

    Теперь: pending==0 проверяется вместе с терминальным состоянием
    пакета, наличие output_file_id проверяется явно, код ответа при
    скачивании проверяется явно, и на нуле картинок состояние удаляется,
    а наверх летит BatchFailed.
    """
    out.mkdir(parents=True, exist_ok=True)
    state = out / "_batch.json"

    def reset(reason):
        """Сносит состояние, чтобы следующий запуск отправил пакет заново."""
        state.unlink(missing_ok=True)
        return BatchFailed(reason)

    if not state.exists():
        lines = [json.dumps({
            "custom_id": f"img_{idx:03d}",
            "method": "POST",
            "url": "/v1/images/generations",
            "body": {"model": model, "prompt": p, "n": 1},
        }) for idx, p in indexed_prompts]
        jsonl = out / "requests.jsonl"
        jsonl.write_text("\n".join(lines))

        up = requests.post(f"{XAI}/files", timeout=TIMEOUT,
                           headers={"Authorization": f"Bearer {key}"},
                           files={"file": open(jsonl, "rb")})
        up.raise_for_status()
        fid = up.json()["id"]

        b = requests.post(f"{XAI}/batches", timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"name": out.parent.name,
                                "input_file_id": fid})
        b.raise_for_status()
        state.write_text(json.dumps(b.json()))
        log(f"  пакет отправлен: {b.json().get('batch_id')}")

    bid = json.loads(state.read_text()).get("batch_id")
    if not bid:
        raise reset("в _batch.json нет batch_id; состояние сброшено, "
                    "перезапусти — пакет уйдёт заново")

    waited, s = 0, {}
    while True:
        r = requests.get(f"{XAI}/batches/{bid}", timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            # 404 значит, что пакета больше нет: он протух или его снесли.
            # Держаться за такое состояние незачем, оно уже никогда не
            # закроется — сбрасываем, чтобы перезапуск отправил новый.
            if r.status_code == 404:
                raise reset(f"пакет {bid} не найден ({r.status_code}); "
                            f"состояние сброшено, перезапусти")
            raise BatchFailed(f"опрос пакета {bid}: {r.status_code} "
                              f"{r.text[:200]}")
        s = r.json()
        st = s.get("state", {}) or {}
        pending = st.get("num_pending", 0)
        if pending == 0:
            break
        if waited >= max_wait:
            # Состояние НЕ трогаем: пакет живой, просто долгий. Следующий
            # запуск подхватит его же и, скорее всего, застанет готовым —
            # платить за него второй раз незачем.
            raise BatchFailed(
                f"пакет {bid} не закрылся за {max_wait // 60} мин "
                f"(в очереди {pending}). Состояние сохранено: запусти "
                f"этап assets ещё раз, пакет будет подхвачен, а не оплачен "
                f"заново",
                alive=True)
        log(f"  в очереди {pending}, жду {poll} сек")
        time.sleep(poll)
        waited += poll

    # НОЛЬ В ОЧЕРЕДИ ЕЩЁ НЕ ЗНАЧИТ УСПЕХ. Ровно так же выглядит пакет,
    # который целиком провалился: заданий в работе не осталось, только
    # все они в num_failed. Раньше эти два случая были неразличимы.
    st = s.get("state", {}) or {}
    done = st.get("num_succeeded", st.get("num_completed", 0))
    failed = st.get("num_failed", 0)
    if failed:
        log(f"  ! в пакете провалилось заданий: {failed}")

    ofid = s.get("output_file_id")
    if not ofid:
        raise reset(
            f"пакет {bid} закрылся без файла результатов "
            f"(готово {done}, провалено {failed}, статус "
            f"{s.get('status') or st.get('status') or '?'}). "
            f"Состояние сброшено, перезапусти")

    resp = requests.get(f"{XAI}/files/{ofid}/content", timeout=TIMEOUT,
                        headers={"Authorization": f"Bearer {key}"})
    if resp.status_code != 200:
        raise reset(f"файл результатов {ofid}: {resp.status_code} "
                    f"{resp.text[:200]}; состояние сброшено, перезапусти")

    n, why = 0, []
    for line in resp.text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            why.append(f"неразбираемая строка: {line[:120]}")
            continue
        cid = rec.get("custom_id") or "?"
        try:
            url = rec["response"]["body"]["data"][0]["url"]
        except Exception:
            # Причина отказа лежит в самой записи, и её надо ПОКАЗАТЬ.
            # Раньше печаталось голое «без результата», по которому нельзя
            # понять ни что случилось, ни что чинить.
            err = (rec.get("error") or {}).get("message") \
                or json.dumps(rec.get("response", rec))[:200]
            log(f"  ! {cid} без результата: {err}")
            why.append(f"{cid}: {err}")
            continue
        img = requests.get(url, timeout=120)
        if img.status_code != 200:
            log(f"  ! {cid}: ссылка отдала {img.status_code}")
            why.append(f"{cid}: ссылка {img.status_code}")
            continue
        (out / f"{cid}.jpg").write_bytes(img.content)
        n += 1

    log(f"  скачано {n} картинок из {len(indexed_prompts)}")
    if n == 0:
        raise reset(
            "пакет не отдал ни одной картинки"
            + (f"; первая причина — {why[0]}" if why else "")
            + ". Состояние сброшено, перезапусти")
    if n < len(indexed_prompts):
        log(f"  ! не хватает {len(indexed_prompts) - n} картинок из пакета "
            f"— их закроет добор в fill_gaps")
    return n


# ────────────────────────── АРХИВЫ ──────────────────────────
# Ключ нужен только Smithsonian. Остальные пять открыты.

def ok(r, name, q):
    """
    Ответ удался? Иначе — В ЛОГ, а не молча.

    Так этот код и подвёл. Ни один источник не смотрел на код ответа, а
    разбирал тело через .get(...) с пустым значением по умолчанию: любой
    401, 403 или 429 превращался в пустой список, неотличимый от честного
    «ничего не нашлось». На прогоне ff-ep03 все 35 клипов приехали с одного
    Pixabay, Pexels не дал ни одного — и в логе об этом не было НИ СЛОВА,
    потому что жаловаться было некому.
    """
    if r.status_code == 200:
        return True
    log(f"    ! {name} «{q}»: ответ {r.status_code} {r.text[:120]}")
    return False


def src_pexels(q, n):
    k = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not k:
        return []
    r = requests.get("https://api.pexels.com/videos/search", timeout=TIMEOUT,
                     headers={"Authorization": k},
                     params={"query": q, "per_page": n, "orientation": "landscape"})
    if not ok(r, "pexels", q):
        return []
    out, dropped, longish = [], 0, 0
    for v in r.json().get("videos", []):
        # 720p-1080p: 4K-рендер весит вчетверо и на 1080p-выходе не виден
        files = [f for f in v["video_files"]
                 if CLIP_MIN_WIDTH <= f.get("width", 0) <= CLIP_MAX_WIDTH
                 and f.get("link")]
        if not files:
            continue
        # у Pexels нет поля тегов, но есть человекочитаемый адрес страницы
        # вида /video/antique-shop-interior-12345 — слова темы лежат в нём
        if not relevant(q, (v.get("url") or "").replace("-", " ")):
            dropped += 1
            continue
        # длинные ролики не берём вовсе: качать минуту ради пятисекундной
        # перебивки — это только вес кэша
        if int(v.get("duration") or 0) > MAX_CLIP_SECONDS * 2:
            longish += 1
            continue
        best = sorted(files, key=lambda f: abs(f["width"] - 1920))[0]
        out.append({"url": best["link"], "src": "pexels",
                    "dur": v.get("duration", 0), "kind": "video"})
    if longish:
        log(f"    pexels «{q}»: отсеяно {longish} длиннее "
            f"{MAX_CLIP_SECONDS * 2} с")
    if dropped:
        log(f"    pexels «{q}»: отсеяно {dropped} не по теме")
    return out


def relevant(query: str, tags: str) -> bool:
    """
    Есть ли у находки хоть одно значимое слово из запроса.

    Стоки ищут по ИЛИ и добирают выдачу чем попало, лишь бы отдать
    запрошенное число. Замер на первом прогоне pawn-01: запрос
    «candle lamp light on aged wood» принёс бананы, петуха с курами,
    помаду, пиво, фейерверк и статую Свободы — двадцать четыре ролика
    из тридцати девяти оказались не по теме, и всё это человеку потом
    отсматривать руками на листе отбора.

    Проверка нарочно мягкая: достаточно ОДНОГО совпадения. Строгая
    (все слова) оставила бы пустую выдачу — у стоков нет столько
    материала по узким запросам. Задача не отобрать лучшее, а отсеять
    заведомо чужое.
    """
    stop = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to",
            "with", "closeup", "close", "up", "detail", "shot", "old"}
    want = {w for w in re.findall(r"[a-z]+", query.lower())
            if len(w) > 2 and w not in stop}
    if not want:
        return True
    have = set(re.findall(r"[a-z]+", (tags or "").lower()))
    return bool(want & have)


def src_pixabay(q, n):
    k = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not k:
        return []
    r = requests.get("https://pixabay.com/api/videos/", timeout=TIMEOUT,
                     params={"key": k, "q": q, "per_page": max(n, 3)})
    if not ok(r, "pixabay", q):
        return []
    out, dropped, longish = [], 0, 0
    for v in r.json().get("hits", []):
        vv = v.get("videos", {})
        # medium у Pixabay это как раз 1280x720, large — 1920x1080.
        # Берём подходящий под потолок, а не самый большой.
        pick = None
        for name in ("large", "medium", "small"):
            f = vv.get(name) or {}
            if f.get("url") and CLIP_MIN_WIDTH <= (f.get("width") or 0) <= CLIP_MAX_WIDTH:
                pick = f
                break
        link = (pick or {}).get("url") or (vv.get("medium") or {}).get("url")
        if not link:
            continue
        if not relevant(q, v.get("tags", "")):
            dropped += 1
            continue
        if int(v.get("duration") or 0) > MAX_CLIP_SECONDS * 2:
            longish += 1
            continue
        out.append({"url": link, "src": "pixabay",
                    "dur": v.get("duration", 0), "kind": "video"})
    if longish:
        log(f"    pixabay «{q}»: отсеяно {longish} длиннее "
            f"{MAX_CLIP_SECONDS * 2} с")
    if dropped:
        log(f"    pixabay «{q}»: отсеяно {dropped} не по теме")
    return out


def src_nasa(q, n, media="image"):
    """Ключ не нужен. Общественное достояние."""
    r = requests.get("https://images-api.nasa.gov/search", timeout=TIMEOUT,
                     headers=UA, params={"q": q, "media_type": media})
    out = []
    for it in r.json().get("collection", {}).get("items", [])[:n * 3]:
        links = it.get("links") or []
        if not links:
            continue
        href = links[0].get("href")
        if href:
            out.append({"url": href, "src": "nasa",
                        "kind": "image" if media == "image" else "video"})
        if len(out) >= n:
            break
    return out


def short_query(q: str, keep: int = 2) -> str:
    """
    Две-три главные слова вместо полной фразы.

    Стоки ищут по ИЛИ и от длинной фразы только шире выдачу. Архивы —
    наоборот, ищут полнотекстово по описанию, и фраза из пяти слов в
    коллекции на несколько тысяч единиц не находит НИЧЕГО. Замер на
    ff-ep03: archive_org и wikimedia_video отдали по нулю на всех
    десяти запросах, ни разу не пожаловавшись — потому что жаловаться
    было не на что, ответ был честный и пустой.
    """
    stop = {"closeup", "close", "up", "detail", "shot", "old", "the", "and",
            "with", "overcast", "dawn", "sunset"}
    ws = [w for w in re.findall(r"[a-zA-Z]+", q.lower())
          if len(w) > 3 and w not in stop]
    return " ".join(ws[:keep]) if ws else q


def src_archive_org(q, n):
    """
    Хроника из archive.org/details/movies.

    ФИЛЬТР ПЕРЕПИСАН. Раньше стояло `licenseurl:(*publicdomain*)` — поле,
    которое у большинства записей просто не заполнено, и источник отдавал
    ноль на каждом запросе (замерено на ff-ep03: archive_org 0 из 94).
    Теперь берём коллекции, которые ЦЕЛИКОМ в общественном достоянии:
    Prelinger — эталонный архив рекламной и бытовой хроники XX века,
    plus явно помеченные publicdomain. Это и честнее по правам, и на
    порядок урожайнее.
    """
    r = requests.get("https://archive.org/advancedsearch.php", timeout=TIMEOUT,
                     headers=UA,
                     params={"q": f'{short_query(q)} AND mediatype:(movies) AND '
                                  f'(collection:(prelinger) OR '
                                  f'collection:(publicmoviescollection) OR '
                                  f'licenseurl:(*publicdomain*))',
                             "fl[]": "identifier", "rows": n * 2,
                             "output": "json"})
    if not ok(r, "archive.org", q):
        return []
    out = []
    # не больше четырёх обращений за метаданными и по 25 секунд на каждое:
    # это самый медленный источник, и на нём легко просидеть минуты
    for d in r.json().get("response", {}).get("docs", [])[:4]:
        ident = d["identifier"]
        meta = requests.get(f"https://archive.org/metadata/{ident}",
                            timeout=25, headers=UA).json()
        # Берём САМЫЙ ЛЁГКИЙ подходящий файл, а не первый попавшийся.
        # В хронике рядом с обзорной нарезкой лежит полнометражная версия
        # на несколько гигабайт, и первым в списке оказывается как повезёт.
        # Один такой файл вешал этап 1 на десятки минут.
        vids = [f for f in meta.get("files", [])
                if f.get("name", "").lower().endswith((".mp4", ".m4v"))]

        def size_of(f):
            try:
                return int(f.get("size") or 0) or MAX_FILE_BYTES * 10
            except (TypeError, ValueError):
                return MAX_FILE_BYTES * 10

        vids = [f for f in sorted(vids, key=size_of) if size_of(f) <= MAX_FILE_BYTES]
        if vids:
            out.append({
                "url": f"https://archive.org/download/{ident}/{vids[0]['name']}",
                "src": "archive.org", "kind": "video"})
        if len(out) >= n:
            break
    return out


def src_artic(q, n):
    """
    Художественный институт Чикаго. Ключ не нужен, отдаёт общественное
    достояние. Предметный музей: по запросу про фарфор отдаёт фарфор,
    а не обмеры зданий, — то самое, чего не хватало от Library of Congress.
    """
    r = requests.get("https://api.artic.edu/api/v1/artworks/search",
                     timeout=TIMEOUT, headers=UA,
                     params={"q": q, "limit": n * 2,
                             "fields": "id,image_id,is_public_domain,title"})
    if not ok(r, "artic", q):
        return []
    out = []
    for it in r.json().get("data", []):
        if not it.get("is_public_domain") or not it.get("image_id"):
            continue
        out.append({
            "url": f"https://www.artic.edu/iiif/2/{it['image_id']}"
                   f"/full/1686,/0/default.jpg",
            "src": "artic", "kind": "image"})
        if len(out) >= n:
            break
    return out


def src_cleveland(q, n):
    """
    Кливлендский музей искусств. Ключ не нужен, cc0=1 отдаёт только то,
    что можно брать без атрибуции. Сильная коллекция прикладного искусства:
    металл, часы, оружие, керамика.
    """
    r = requests.get("https://openaccess-api.clevelandart.org/api/artworks/",
                     timeout=TIMEOUT, headers=UA,
                     params={"q": q, "cc0": 1, "has_image": 1, "limit": n * 2})
    if not ok(r, "cleveland", q):
        return []
    out = []
    for it in r.json().get("data", []):
        img = ((it.get("images") or {}).get("web") or {}).get("url")
        if img:
            out.append({"url": img, "src": "cleveland", "kind": "image"})
        if len(out) >= n:
            break
    return out


def src_nasa_video(q, n):
    """
    images.nasa.gov, видео. Ключ не нужен, всё в общественном достоянии.

    Для канала про древности это боковой источник: НАСА отдаёт космос и
    технику. Держим его доступным по имени, но в умолчания не ставим — по
    предметным запросам он даёт шум, и это уже проверено на фотографиях.
    """
    r = requests.get("https://images-api.nasa.gov/search", timeout=TIMEOUT,
                     headers=UA, params={"q": q, "media_type": "video"})
    if not ok(r, "nasa_video", q):
        return []
    out = []
    for it in (r.json().get("collection", {}).get("items") or [])[:n * 2]:
        href = it.get("href")
        if not href:
            continue
        try:
            files = requests.get(href, timeout=30, headers=UA).json()
        except Exception:
            continue
        # в списке лежат рендеры разного размера; берём мобильный/средний,
        # оригиналы бывают по несколько гигабайт
        pick = [f for f in files if f.endswith(".mp4") and "~mobile" in f] or \
               [f for f in files if f.endswith(".mp4")]
        if pick:
            out.append({"url": pick[0], "src": "nasa", "kind": "video"})
        if len(out) >= n:
            break
    return out


def src_met(q, n):
    """Met Museum. Ключ не нужен, только объекты в открытом доступе."""
    r = requests.get("https://collectionapi.metmuseum.org/public/collection/"
                     "v1/search", timeout=TIMEOUT, headers=UA,
                     params={"q": q, "hasImages": "true", "isPublicDomain": "true"})
    if not ok(r, "met", q):
        return []
    out = []
    for oid in (r.json().get("objectIDs") or [])[:n * 2]:
        o = requests.get("https://collectionapi.metmuseum.org/public/"
                         f"collection/v1/objects/{oid}",
                         timeout=TIMEOUT, headers=UA).json()
        img = o.get("primaryImage")
        if img and o.get("isPublicDomain"):
            out.append({"url": img, "src": "met", "kind": "image"})
        if len(out) >= n:
            break
    return out


def src_loc(q, n):
    """Библиотека Конгресса. Ключ не нужен."""
    r = requests.get("https://www.loc.gov/photos/", timeout=TIMEOUT, headers=UA,
                     params={"q": q, "fo": "json", "c": n * 2})
    if not ok(r, "loc", q):
        return []
    out = []
    for it in r.json().get("results", [])[:n * 2]:
        imgs = it.get("image_url") or []
        if imgs:
            out.append({"url": imgs[-1], "src": "loc", "kind": "image"})
        if len(out) >= n:
            break
    return out


def src_wikimedia(q, n):
    """Commons. Ключ не нужен, но User-Agent обязателен."""
    r = requests.get("https://commons.wikimedia.org/w/api.php", timeout=TIMEOUT,
                     headers=UA,
                     params={"action": "query", "generator": "search",
                             "gsrsearch": f"{q} filetype:bitmap",
                             "gsrlimit": n * 2, "prop": "imageinfo",
                             "iiprop": "url|extmetadata", "iiurlwidth": 1920,
                             "format": "json"})
    if not ok(r, "wikimedia", q):
        return []
    out = []
    for page in (r.json().get("query", {}).get("pages", {}) or {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        lic = ((ii.get("extmetadata") or {}).get("LicenseShortName", {})
               .get("value", "")).lower()
        if not any(t in lic for t in ("public domain", "cc0", "pd-")):
            continue          # атрибуцию не берём принципиально
        url = ii.get("thumburl") or ii.get("url")
        if url:
            out.append({"url": url, "src": "wikimedia", "kind": "image"})
        if len(out) >= n:
            break
    return out


def src_wikimedia_video(q, n):
    """
    Хроника с Commons. Ключа не нужно, но нужен User-Agent.

    Добавлен, потому что видеостоков по узким историческим темам мало:
    на ff-ep03 из трёх заявленных источников материал дал ровно один.
    Commons отдаёт webm и ogv — ffmpeg их читает, а дальше всё равно идёт
    перекодирование в общий формат, так что контейнер значения не имеет.
    """
    r = requests.get("https://commons.wikimedia.org/w/api.php", timeout=TIMEOUT,
                     headers=UA,
                     params={"action": "query", "generator": "search",
                             "gsrsearch": f"{short_query(q)} filetype:video",
                             "gsrlimit": n * 2, "prop": "imageinfo",
                             "iiprop": "url|extmetadata|size",
                             "format": "json"})
    if not ok(r, "wikimedia_video", q):
        return []
    out = []
    for page in (r.json().get("query", {}).get("pages", {}) or {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        lic = ((ii.get("extmetadata") or {}).get("LicenseShortName", {})
               .get("value", "")).lower()
        if not any(t in lic for t in ("public domain", "cc0", "pd-")):
            continue
        url = ii.get("url")
        if not url or not url.lower().endswith((".webm", ".ogv", ".mp4")):
            continue
        # тяжёлые файлы отсекаем здесь: на Commons рядом с нарезкой лежат
        # оцифровки целых катушек на сотни мегабайт
        if int(ii.get("size") or 0) > MAX_FILE_BYTES:
            continue
        out.append({"url": url, "src": "wikimedia", "kind": "video"})
        if len(out) >= n:
            break
    return out


def _magnific_quota_path() -> Path:
    return Path("work") / "_magnific_daily.json"


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _magnific_quota_used() -> int:
    """
    Сколько единиц уже взято из стока Magnific СЕГОДНЯ.

    Файл общий на все ролики канала, не на один work/<id>: квота — условие
    самого API-ключа, а не отдельного ролика. Веди счёт per-job — и день с
    тремя сборками подряд потратит втрое больше квоты, чем API реально даёт.
    """
    p = _magnific_quota_path()
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return 0
    if d.get("date") != _today():
        return 0
    return int(d.get("used", 0))


def _magnific_quota_add(n: int) -> int:
    p = _magnific_quota_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    used = _magnific_quota_used() + n
    p.write_text(json.dumps({"date": _today(), "used": used}))
    return used


def src_magnific(q, n):
    """
    Сток Magnific — не источник наравне с остальными десятью, а узкая
    заплатка: уникальные векторы, графика и иллюстрации, которых нет ни у
    шести открытых архивов, ни у футажных стоков выше. Поэтому его нет ни в
    VIDEO_SOURCES, ни в PHOTO_SOURCES — вызывается отдельно из
    fetch_material, и только на запросы, по которым обычные источники
    вернули пусто.

    ПОТОЛОК 10-15 ЕДИНИЦ В СУТКИ, ОБЩИЙ НА ВИДЕО И ФОТО (MAGNIFIC_STOCK_
    DAILY_CAP). Хранится в work/_magnific_daily.json и переживает
    пересборки одного дня: ролику нужно пять-десять пересборок монтажа, и
    если бы счётчик обнулялся на каждой, суточный лимит стал бы лимитом на
    прогон.

    ЭНДПОЙНТ НЕПОДТВЕРЖДЁН. Документация канала называет только страницу
    каталога («GET /api-reference/resources») без реального пути вызова —
    это адрес СТРАНИЦЫ документации (по образцу /api-reference/mystic/
    post-mystic для Mystic, чей настоящий путь вызова /v1/ai/mystic
    оказался другим), а не подтверждённый REST-путь. Ниже — рабочее
    предположение по аналогии с остальным API; сверить перед первым
    платным запросом. Синхронный (без task_id): раздел 3 документации
    относит к асинхронным именно AI-генерацию, поиск по стоку — обычная
    выдача каталога.
    """
    key = (os.environ.get("MAGNIFIC_API_KEY") or "").strip()
    if not key:
        return []
    used = _magnific_quota_used()
    room = MAGNIFIC_STOCK_DAILY_CAP - used
    if room <= 0:
        log(f"    magnific: суточная квота {MAGNIFIC_STOCK_DAILY_CAP} "
            f"исчерпана, пропускаю «{q}»")
        return []
    take = min(n, room)
    r = requests.get(f"{MAGNIFIC_API}/v1/resources", timeout=TIMEOUT,
                     headers=_magnific_headers(key),
                     params={"query": q, "type": "vector,illustration,graphic",
                             "per_page": take})
    if not ok(r, "magnific", q):
        return []
    out = []
    for it in r.json().get("results", [])[:take]:
        url = it.get("url") or it.get("download_url")
        if not url:
            continue
        kind = "video" if it.get("media_type") == "video" else "image"
        out.append({"url": url, "src": "magnific", "kind": kind})
    if out:
        _magnific_quota_add(len(out))
        log(f"    magnific: взято {len(out)}, квота на сегодня "
            f"{used + len(out)}/{MAGNIFIC_STOCK_DAILY_CAP}")
    return out


# Источники по умолчанию. Список СВОЙ У КАНАЛА и задаётся в спецификации
# полями video_sources / photo_sources — здесь только умолчание.
#
# NASA из фото убрана. Это космическое агентство: на канале про древности
# по любому запросу оно отдаёт снимки Земли и техники, то есть чистый шум.
# В исходном проекте (научно-исторический канал) она была на месте.
#
# Замер на первом прогоне pawn-01, 40 архивных фото: Met дал почти весь
# годный материал — предметы, мебель, часы, живопись. Library of Congress
# отдал в основном обложки книг и обмеры зданий, годного меньше половины.
# Wikimedia не дала ничего: фильтр лицензий отсекает почти всё, что она
# находит по предметным запросам.
ALL_SOURCES = {
    # видео
    "pexels": src_pexels,
    "pixabay": src_pixabay,
    "archive_org": src_archive_org,
    "wikimedia_video": src_wikimedia_video,
    "nasa_video": src_nasa_video,
    # фото
    "met": src_met,
    "artic": src_artic,
    "cleveland": src_cleveland,
    "loc": src_loc,
    "wikimedia": src_wikimedia,
    "nasa": src_nasa,
    # заплатка, не общий источник — см. src_magnific
    "magnific": src_magnific,
}

VIDEO_SOURCES = [src_pexels, src_pixabay, src_archive_org, src_wikimedia_video]
# Met, Чикаго и Кливленд идут первыми: все три — предметные музеи, и по
# предметному запросу отдают предмет. Library of Congress оставлен следом,
# но он же и главный поставщик книжных обложек, которые потом бракует зрение.
PHOTO_SOURCES = [src_met, src_artic, src_cleveland, src_loc, src_wikimedia]


def sources_from(job, key, default):
    """Источники по именам из спецификации. Опечатка роняет сразу, со списком."""
    names = job.get(key)
    if not names:
        return default
    bad = [n for n in names if n not in ALL_SOURCES]
    if bad:
        raise SystemExit(
            f"{key}: нет таких источников " + ", ".join(bad) +
            "\nесть: " + ", ".join(sorted(ALL_SOURCES)))
    return [ALL_SOURCES[n] for n in names]


def fetch(url, dst: Path, limit=MAX_FILE_BYTES, seconds=FETCH_SECONDS):
    """
    Качает файл потоком, с потолком и по размеру, и по времени.

    Скачивать через .content нельзя: файл целиком уезжает в память, а
    оборвать раздувшуюся загрузку нечем. Здесь и то, и другое под контролем,
    а недокачанный файл удаляется — обрезанное видео дальше по конвейеру
    хуже, чем его отсутствие.
    """
    stop = time.time() + seconds
    try:
        r = requests.get(url, headers=UA, stream=True, timeout=(15, 30))
        if r.status_code != 200:
            return False
        size = int(r.headers.get("Content-Length") or 0)
        if size > limit:
            log(f"  ! пропускаю, {size // 1048576} МБ — тяжелее потолка")
            return False
        n = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                n += len(chunk)
                if n > limit or time.time() > stop:
                    raise TimeoutError(
                        f"{n // 1048576} МБ / {seconds} с — не уложился")
                f.write(chunk)
        if n < 20000:                     # заглушка вместо файла
            dst.unlink(missing_ok=True)
            return False
        return True
    except Exception as e:
        dst.unlink(missing_ok=True)
        log(f"  ! скачать не вышло: {e}")
        return False


def gather(queries, per_query, sources, out: Path, kind, budget=GATHER_BUDGET):
    """
    Обходит источники и качает материал, укладываясь в отведённое время.

    Бюджет обязателен. Раньше его не было, и один медленный файл с
    archive.org держал этап 1 больше получаса: requests.timeout ограничивает
    паузу МЕЖДУ байтами, а не всю загрузку, поэтому сервер, отдающий данные
    тонкой струйкой, не срабатывает по таймауту никогда.

    Материала всегда больше, чем нужно ролику, так что оборваться на
    середине списка не страшно — важно не встать намертво.

    ДОБОР. Функция запускается повторно, чтобы дозакачать материал по
    исправленным запросам, поэтому нумерация продолжается с того места, где
    кончилась, а не начинается с нуля. Иначе второй заход переписал бы
    clip_000 другим содержимым — и номера, которые человек отметил на листе
    отбора, стали бы указывать на другие файлы. Уже скачанные ссылки
    пропускаются: платить временем за то же самое незачем.
    """
    out.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + budget

    old = []
    man = out / "_manifest.json"
    if man.exists():
        try:
            old = json.loads(man.read_text())
        except json.JSONDecodeError:
            old = []
    seen = {o.get("url") for o in old if o.get("url")}
    # следующий номер берём с диска, а не из манифеста: файл мог быть
    # положен руками или манифест мог потеряться вместе с кэшем
    have = [int(p.name.split("_")[1]) for p in out.glob(f"{kind}_*")
            if p.name.split("_")[1].isdigit()]
    n = max(have) + 1 if have else 0
    if old or have:
        log(f"  уже есть {len(have)} шт, продолжаю с номера {n:03d}")

    got = []
    by_src = {}
    for q in queries:
        for fn in sources:
            if time.time() > deadline:
                break
            try:
                items = fn(q, per_query)
            except Exception as e:
                log(f"  ! {fn.__name__} на «{q}»: {e}")
                by_src.setdefault(fn.__name__[4:], 0)
                continue
            by_src[fn.__name__[4:]] = by_src.get(fn.__name__[4:], 0) + len(items)
            for it in items:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                if time.time() > deadline:
                    log(f"  … время на «{kind}» вышло, беру что успел")
                    break
                ext = ".mp4" if it["kind"] == "video" else ".jpg"
                dst = out / f"{kind}_{n:03d}_{it['src']}{ext}"
                if fetch(it["url"], dst):
                    # Видео подрезаем СРАЗУ. Дальше файл живёт в кэше между
                    # прогонами, и лишние секунды в нём — это лишние
                    # мегабайты на каждой пересборке.
                    if it["kind"] == "video":
                        trim_long_clip(dst)
                    # запрос сохраняется рядом с файлом: по нему build.py потом
                    # подбирает кадр под то, что звучит в эту секунду
                    got.append({"file": str(dst), "q": q, **it})
                    log(f"  {kind} {n:03d}: {it['src']}  «{q}»")
                    n += 1
            if time.time() > deadline:
                break
        if time.time() > deadline:
            break
    man.write_text(json.dumps(old + got, indent=1))
    # Сколько предложил КАЖДЫЙ источник. Источник, стабильно отдающий ноль,
    # — это либо мёртвый ключ, либо запросы не того словаря, и то и другое
    # чинится только когда видно. Раньше на весь сбор была одна строка
    # «добавлено 35», по которой это было неотличимо.
    if by_src:
        log("  по источникам: " + ", ".join(
            f"{s} {c}" for s, c in sorted(by_src.items(), key=lambda x: -x[1])))
    log(f"  {kind}: добавлено {len(got)}, всего {len(old) + len(got)}")
    return got


# ────────────────────────── ПРОВЕРКА КЛЮЧЕЙ ──────────────────────────

# Ключ есть и он настоящий, просто выдан с урезанными правами.
# Проверяется ПЕРВЫМ: в таком ответе есть и слово authentication, и 401.
SCOPE_HINTS = ("missing_permissions", "missing the permission")

# Недвусмысленный отказ именно по ключу. Список намеренно узкий: всё, что
# сюда не попало, считается непонятным ответом и лишь печатается.
DENY_HINTS = ("invalid_api_key", "invalid api key", "incorrect api key",
              "invalid authentication", "no api key", "api key not found")


def check_keys():
    """
    Дёргает по одному дешёвому запросу на каждый ключ ДО того, как начнётся
    озвучка и генерация. Иначе неверный ключ вылезает на первом же обращении,
    и каждый следующий ключ проверяется отдельным прогоном по четверти часа.

    Все пять запросов бесплатные и ничего не создают. Значения ключей никуда
    не печатаются — только вердикт и текст ответа сервиса.
    """
    el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()  # необязателен
    xai = os.environ.get("XAI_API_KEY", "").strip()
    pex = os.environ.get("PEXELS_API_KEY", "").strip()
    pix = os.environ.get("PIXABAY_API_KEY", "").strip()
    magnific = os.environ.get("MAGNIFIC_API_KEY", "").strip()  # необязателен

    bad = []

    def probe(name, url, headers=None, params=None):
        """Возвращает True, если сервис принял ключ."""
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            log(f"  ? {name}: сеть недоступна ({e}) — проверить не удалось")
            return None
        if r.status_code == 200:
            log(f"  + {name}: принят")
            return True

        low = r.text.lower()

        # Урезанные права — НЕ повод останавливать сборку. Ключ ElevenLabs
        # можно выдать без user_read или voices_read, и он при этом отлично
        # озвучивает. Ровно на этом проверка один раз остановила прогон,
        # который до неё собирал ролик без единой жалобы.
        if any(h in low for h in SCOPE_HINTS):
            log(f"  + {name}: принят (права ключа урезаны, для работы хватает)")
            return True

        # По коду судить нельзя: xAI на неверный ключ отвечает 400
        # («Incorrect API key provided»), а не 401. Смотрим, что сервис
        # сказал про сам ключ. Тело печатаем ВСЕГДА: без него код ответа
        # ничего не объясняет.
        if any(h in low for h in DENY_HINTS):
            log(f"  ! {name}: ОТКАЗ {r.status_code} — {r.text[:220]}")
            bad.append(name)
            return False

        # Всё остальное — только предупреждение. Проверка не должна
        # останавливать работающий пайплайн из-за ответа, которого не поняла.
        log(f"  ? {name}: ответ {r.status_code}, разбираться не берусь — "
            f"{r.text[:200]}")
        return None

    el_ok = probe("ELEVENLABS_API_KEY", "https://api.elevenlabs.io/v1/user",
                  {"xi-api-key": el})
    # voice_id проверяется тем же ключом. Если ключ не принят, проверка голоса
    # вернёт тот же 401 и обвинит исправный voice_id — поэтому пропускаем.
    #
    # Пустой голос здесь не ошибка: штатно он задаётся полем voice_id в
    # спецификации ролика, а секрет остался запасным путём. Опрашивать
    # /v1/voices/ с пустым идентификатором нельзя — ответ на такой запрос
    # ничего не говорит о ключе, а в лог уйдёт ложная жалоба.
    if not voice:
        log("  . ELEVENLABS_VOICE_ID: не задан, голос берётся из спецификации")
    elif el_ok:
        probe("ELEVENLABS_VOICE_ID",
              f"https://api.elevenlabs.io/v1/voices/{voice}", {"xi-api-key": el})
    else:
        log("  . ELEVENLABS_VOICE_ID: не проверен, сначала нужен рабочий ключ")

    probe("XAI_API_KEY", "https://api.x.ai/v1/models",
          {"Authorization": f"Bearer {xai}"})
    # ПРОВЕРЯЕМ ИМЕННО ВИДЕО. Раньше здесь стоял /v1/search — поиск по
    # фотографиям, которым мы не пользуемся вовсе: Pexels берётся только под
    # футаж. Ключ отвечал на фото 200 «принят», а на /videos/search — 401
    # «Invalid API key», и проверка бодро рапортовала об исправном ключе,
    # пока источник не отдавал ни одного ролика. Проверять надо ту дверь,
    # в которую собираешься входить.
    probe("PEXELS_API_KEY", "https://api.pexels.com/videos/search",
          {"Authorization": pex}, {"query": "test", "per_page": 1})
    probe("PIXABAY_API_KEY", "https://pixabay.com/api/",
          None, {"key": pix, "q": "test", "per_page": 3})

    # НЕОБЯЗАТЕЛЕН, но не молча: если ключ задан и отклонён — это дырка на
    # 70% картинок ролика. Если ключа просто нет — генерация целиком уходит
    # на xAI, футаж и графика через magnific не добираются — штатный режим
    # для канала, где Magnific ещё не подключён.
    #
    # НЕ через общий probe(): нет подтверждённого дешёвого GET-эндпойнта для
    # проверки ключа без побочных эффектов (аналитика в документации помечена
    # «для командных/enterprise аккаунтов» — рабочему ключу без такого
    # тарифа она может честно ответить отказом, и probe() принял бы это за
    # мёртвый ключ и уронил бы прогон). Поэтому только информационный запрос,
    # который никогда не останавливает сборку — окончательная проверка ключа
    # всё равно происходит на первом реальном вызове images_magnific.
    if magnific:
        try:
            r = requests.get(f"{MAGNIFIC_API}/v1/analytics/team-api-keys",
                             headers=_magnific_headers(magnific), timeout=30)
            if r.status_code == 200:
                log("  + MAGNIFIC_API_KEY: принят")
            else:
                log(f"  ? MAGNIFIC_API_KEY: ответ {r.status_code} на "
                    f"проверочный запрос (эндпойнт для командных тарифов, "
                    f"отказ здесь не обязательно значит мёртвый ключ) — "
                    f"{r.text[:160]}")
        except Exception as e:
            log(f"  ? MAGNIFIC_API_KEY: сеть недоступна ({e}) — "
                f"проверить не удалось")
    else:
        log("  . MAGNIFIC_API_KEY: не задан — 100% генерации картинок "
            "уйдёт на xAI, футаж и векторы через magnific добираться не будут")

    if bad:
        raise SystemExit(
            "Сервисы не приняли ключи: " + ", ".join(bad) + ".\n"
            "Значения лежат в Settings -> Secrets and variables -> Actions.\n"
            "Чаще всего это устаревший ключ или значения, перепутанные местами\n"
            "между секретами. Ключ ElevenLabs начинается с sk_, voice_id — это\n"
            "короткий идентификатор голоса из Voice Library, а не ключ.")


# ─────────────────── УРОЖАЙНОСТЬ И ДОЗАКАЧКА ───────────────────

def manifest_of(work: Path, folder: str):
    p = work / folder / "_manifest.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []


def _file_number(path_str: str):
    """clip_007_pexels.mp4 -> 7. Номер — то, чем оперирует отбраковка."""
    try:
        return int(Path(path_str).name.split("_")[1])
    except (IndexError, ValueError):
        return None


def yield_report(work: Path, folder: str, kind: str, log=log):
    """
    Сколько КАЖДЫЙ запрос скачал и сколько из этого выжило после отбраковки.

    Раньше в логе была строка «по источникам: pixabay 12, pexels 0» — по ней
    видно мёртвый ИСТОЧНИК, но не видно мёртвый ЗАПРОС. А чинить надо
    именно запрос: на первом прогоне pawn-01 «candle lamp light on aged
    wood» принёс бананы, петуха и статую Свободы — всё честно скачалось,
    всё честно отбраковалось, и в логе это выглядело как общая убыль.

    Возвращает {запрос: (скачано, выжило)}. Запрос с нулём выживших — тот
    самый, который надо переписать, и теперь его видно поимённо.
    """
    man = manifest_of(work, folder)
    if not man:
        return {}
    rejected = set(vet.rejected_from(work).get(kind, []))
    stat = {}
    for item in man:
        q = item.get("q")
        if not q:
            continue
        got, alive = stat.get(q, (0, 0))
        n = _file_number(item.get("file", ""))
        stat[q] = (got + 1, alive + (0 if n in rejected else 1))

    dead = [q for q, (g, a) in stat.items() if a == 0]
    log(f"  урожайность запросов ({folder}):")
    for q, (g, a) in sorted(stat.items(), key=lambda x: x[1][1]):
        mark = "  ← ПУСТО" if a == 0 else ""
        log(f"    {a:>2}/{g:<3} «{q}»{mark}")
    if dead:
        n = len(dead)
        word = ("запрос не дал" if n % 10 == 1 and n % 100 != 11
                else "запроса не дали" if n % 10 in (2, 3, 4)
                and n % 100 not in (12, 13, 14) else "запросов не дали")
        log(f"  ! {n} {word} НИ ОДНОГО годного файла — "
            f"их надо переписать, а не добирать: " +
            ", ".join(f"«{q}»" for q in dead[:4]) +
            (" …" if n > 4 else ""))
    return stat


# Как расширяется мёртвый запрос. Ровно две операции, и обе безопасные:
# укоротить до главных слов (стоки ищут по ИЛИ, длинная фраза только шире
# размывает выдачу) и снять уточнения фактуры, оставив предмет.
#
# Синонимов и переводов здесь НЕТ намеренно. Подстановка синонимов ведёт
# запрос в сторону от темы — а именно уход от темы и есть причина, по
# которой запрос уже отбраковался. Лучше меньше материала, чем материал
# не про то: второе стоит человеку отсмотра, первое — одной строки в логе.
def expand_query(q: str):
    """Варианты одного запроса, от самого близкого к исходному."""
    out, seen = [], {q.lower()}
    for keep in (3, 2):
        v = short_query(q, keep)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def top_up_footage(job, work: Path, need_clips: int, log=log):
    """
    Дозакачка футажа, когда годного меньше, чем просит план.

    Зачем автоматически. rails.py умеет сказать «155 кадров подряд без
    единой вставки видео», но говорит это ПОСЛЕ раскладки, и дальше цикл
    такой: человек видит предупреждение, руками правит запросы, гоняет
    stage: material заново. При этом сама нехватка известна здесь и сейчас —
    сразу после отбраковки, до того как что-либо разложено.

    Слайдшоу — это не спорное стилевое решение, которое нельзя чинить
    автоматически (принцип rails.py), а нехватка сырья. Разница
    принципиальная: правка под метрику меняла бы РЕШЕНИЕ монтажёра, а здесь
    доливается МАТЕРИАЛ, и все решения остаются за редакторским слоем.

    Денег не стоит: те же открытые источники, что и в обычном сборе.
    """
    man = manifest_of(work, "footage")
    rejected = set(vet.rejected_from(work).get("clip", []))
    alive = sum(1 for m in man
                if _file_number(m.get("file", "")) not in rejected)
    if alive >= need_clips:
        log(f"  футажа хватает: годных {alive} при нужных {need_clips}")
        return 0

    stat = {}
    for m in man:
        q = m.get("q")
        if not q:
            continue
        n = _file_number(m.get("file", ""))
        g, a = stat.get(q, (0, 0))
        stat[q] = (g + 1, a + (0 if n in rejected else 1))

    # Расширяем ТОЛЬКО те запросы, что уже что-то дали: у них тема ловится,
    # просто выдача узкая. Запрос с нулём годного расширять бессмысленно —
    # он не про то, и короткая его версия будет не про то же самое.
    live_q = [q for q, (g, a) in stat.items() if a > 0] or \
             list(job.get("footage_queries", []))
    extra = []
    for q in live_q:
        extra += expand_query(q)
    # порядок сохраняем, дубли убираем
    seen, queries = set(), []
    for q in extra:
        if q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    if not queries:
        log(f"  ! футажа не хватает ({alive} из {need_clips}), но расширять "
            f"нечего: ни один запрос не дал годного материала")
        return 0

    log(f"  ! годного футажа {alive}, плану нужно {need_clips} — "
        f"дозакачка по {len(queries)} расширенным запросам")
    vids = sources_from(job, "video_sources", VIDEO_SOURCES)
    before = len(man)
    gather(queries, 3, vids, work / "footage", "clip",
           budget=int(job.get("top_up_budget", 240)))
    added = len(manifest_of(work, "footage")) - before
    log(f"  дозакачано {added} клипов — их ещё предстоит отбраковать")
    return added


def clips_needed(job, total_seconds: float) -> int:
    """
    Сколько РАЗНЫХ клипов просит план, чтобы не выйти слайдшоу.

    Считается тем же способом, что и в build.plan_shots: доля стока от
    экранного времени, делённая на среднюю длину кадра, и всё это делится
    на потолок повторов одного файла (build.MAX_REUSE = 3). Точность здесь
    не нужна: это порог «доливать или нет», а не план.
    """
    base = float((job.get("style_override") or {}).get(
        "base_duration_range", [4.2, 5.6])[0]) or 4.8
    share = float((job.get("style_override") or {}).get("generated_share", 0.30))
    shots = max(8, int(total_seconds / base))
    # сток закрывает примерно половину «реальной» части, вторая половина —
    # архивные фото; ровно так же делит их MaterialMix на монтаже
    clip_shots = shots * (1 - share) * 0.5
    return max(4, int(clip_shots / 3))


# ────────────────────────── ГЛАВНОЕ ──────────────────────────

def fetch_material(job, work: Path):
    """
    Только футаж и архивные фото. Ни озвучки, ни генерации — денег не тратит.

    Вынесено отдельно, потому что материал приходится добирать: запросы
    правятся после того, как посмотришь, что по ним нашлось, и гонять ради
    этого заново озвучку за деньги незачем.
    """
    vids = sources_from(job, "video_sources", VIDEO_SOURCES)
    phot = sources_from(job, "photo_sources", PHOTO_SOURCES)
    # ЗАПАС 40%. Робот отбраковывает материал сам (vet.py), и часть подборки
    # заведомо уйдёт в брак — на первом прогоне ушло две трети стока. Качать
    # ровно столько, сколько нужно ролику, значит остаться без материала уже
    # после отбраковки. Перебор ничего не стоит: лишнее просто не попадёт в
    # монтаж, а нехватка означает повторный прогон.
    #
    # У ВИДЕО ЗАПАС ВЫШЕ, чем у фото (7, а не 3 в базе). Замер на ff-ep03:
    # отбраковка съела 32 клипа из 35 — годными остались три штуки на весь
    # получасовой ролик, и их пришлось крутить по кругу десятки раз. У фото
    # выход куда лучше (на том же прогоне годных было две трети), поэтому
    # множитель для архива не трогаем.
    # БАЗА ВИДЕО ПОДНЯТА С 7 ДО 12 по замеру на ff-ep06: отбраковка съела
    # 159 клипов из 179 (89%), и на 29 слотов видео осталось 20 файлов —
    # оттуда и «19 кадров-картинок подряд» в проверке плана, и подводные
    # аквалангисты под историей про английское поле. При 7 x 1.4 = 10 на
    # запрос арифметика не сходилась изначально: чтобы после 89% отсева
    # остался хотя бы десяток годных клипов, скачать надо под сотню.
    #
    # Фото не трогаем: там отсев 11 из 49 (22%), запас 4 x 1.4 достаточен.
    # Стоит это только времени скачивания — все шесть архивов и оба стока
    # бесплатны, денег этап не тратит.
    over = float(job.get("material_overshoot", 1.4))
    log("── футажи (" + ", ".join(f.__name__[4:] for f in vids) +
        f", запас x{over:g})")
    gather(job["footage_queries"], max(1, round(12 * over)), vids,
           work / "footage", "clip")
    log("── реальные фото из архивов (" +
        ", ".join(f.__name__[4:] for f in phot) + ")")
    gather(job["archive_queries"], max(1, round(4 * over)), phot,
           work / "archive", "arch")

    # ВЕКТОРЫ, ГРАФИКА, ИЛЛЮСТРАЦИИ — отдельный список, необязательный.
    # Только если ролику они реально нужны (поле graphic_queries непусто).
    # Сначала пробуем обычные архивы — вдруг что-то найдётся честно; то, что
    # не нашлось НИГДЕ, добирает Magnific, и только в пределах суточной
    # квоты (см. src_magnific).
    graphic_q = job.get("graphic_queries") or []
    if graphic_q:
        log(f"── графика/векторы/иллюстрации ({len(graphic_q)} запросов)")
        gather(graphic_q, max(1, round(3 * over)), phot, work / "archive", "arch")
        man = work / "archive" / "_manifest.json"
        found = ({a.get("q") for a in json.loads(man.read_text())}
                 if man.exists() else set())
        missing = [q for q in graphic_q if q not in found]
        if missing:
            log(f"  ! {len(missing)} тем не нашлось в обычных архивах, "
                f"добираю уникальные векторы/иллюстрации через magnific "
                f"(потолок {MAGNIFIC_STOCK_DAILY_CAP}/сутки)")
            gather(missing, 2, [src_magnific], work / "archive", "arch")

    fill_missing_footage_via_magnific(job, work)


def fill_missing_footage_via_magnific(job, work: Path):
    """
    Короткие вставки (2-3 с) через Magnific — туда, где реального футажа по
    теме не нашлось НИ НА ОДНОМ стоке и архиве вообще. Не замена материалу,
    а заплатка на конкретную дыру, и заплатка ограниченная: не больше 5%
    клипов футажа ролика (MAGNIFIC_VIDEO_GEN_SHARE) — 60-70% экранного
    времени должны остаться реальным материалом, как было оговорено раньше.

    ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ (magnific_video_gen_enabled=false), и это не
    перестраховка ради перестраховки. Документация канала описывает
    видео-генерацию Magnific в разделе «image-to-video» — то есть на входе
    ожидается ИСХОДНАЯ КАРТИНКА, которую движок оживляет движением, а не
    голый текстовый промпт. Здесь ниже запрос уходит с одним «prompt», без
    исходного изображения — это может значить как «параметр не тот»,
    так и «весь метод вызова не тот» (сначала сгенерировать кадр через
    Mystic, потом отдать его в Kling/MiniMax/WAN). Включать поле явно —
    после того, как путь и параметры сверены с
    /api-reference/image-to-video/<engine>/post-<engine> — иначе первый
    же платный вызов рискует потратить кредиты впустую или тихо вернуть
    не то, что ожидалось.
    """
    key = (os.environ.get("MAGNIFIC_API_KEY") or "").strip()
    if not key:
        return
    if not job.get("magnific_video_gen_enabled", False):
        return
    man_path = work / "footage" / "_manifest.json"
    man = json.loads(man_path.read_text()) if man_path.exists() else []
    real_q = {m.get("q") for m in man if m.get("src") != "magnific_video_gen"}
    missing_q = [q for q in job.get("footage_queries", []) if q not in real_q]
    if not missing_q:
        return

    share = float(job.get("magnific_video_share", MAGNIFIC_VIDEO_GEN_SHARE))
    # Знаменатель — уже скачанный реальный футаж, а не план кадров: план
    # складывается позже, в build.py, и здесь его ещё нет. Приближение
    # грубое, но потолок и должен быть с запасом консервативным, а не точным
    # до клипа.
    total_clips = len(man) or len(job.get("footage_queries", []))
    cap = max(0, round(total_clips * share))
    already = sum(1 for m in man if m.get("src") == "magnific_video_gen")
    room = cap - already
    if room <= 0:
        log(f"  ! футажа не нашлось по {len(missing_q)} темам, но потолок "
            f"генерации через magnific ({cap} клипов, {share*100:.0f}% "
            f"от {total_clips}) уже исчерпан")
        return

    take = missing_q[:room]
    log(f"  ! реального футажа не нашлось по {len(missing_q)} темам; "
        f"генерирую короткие вставки через magnific ({len(take)} шт, "
        f"потолок {share*100:.0f}%)")
    out = work / "footage"
    have = [int(p.name.split("_")[1]) for p in out.glob("clip_*")
            if p.name.split("_")[1].isdigit()]
    n = max(have) + 1 if have else 0
    got = []
    for i, q in enumerate(take):
        model = MAGNIFIC_VIDEO_MODELS[i % len(MAGNIFIC_VIDEO_MODELS)]
        dst = out / f"clip_{n:03d}_magnific.mp4"
        # НЕПОДТВЕРЖДЁННЫЙ ПУТЬ И ПАРАМЕТРЫ — см. предупреждение в шапке
        # функции. /v1/ai/<engine> — перенос схемы Mystic по аналогии.
        r = requests.post(f"{MAGNIFIC_API}/v1/ai/{model}", timeout=TIMEOUT,
                          headers=_magnific_headers(key),
                          json={"prompt": q, "duration_seconds": 2.5})
        if r.status_code != 200:
            log(f"  ! magnific видео «{q}» ({model}) не запустилось: "
                f"{r.status_code} {r.text[:160]}")
            continue
        task_id = (r.json().get("data") or {}).get("task_id")
        if not task_id:
            log(f"  ! magnific видео «{q}»: в ответе нет task_id")
            continue
        try:
            data = _magnific_poll(f"/v1/ai/{model}", task_id, key, timeout=600)
        except (RuntimeError, TimeoutError) as e:
            log(f"  ! magnific видео «{q}» ({model}): {e}")
            continue
        url = _magnific_generated_url(data)
        if not url:
            log(f"  ! magnific видео «{q}»: в ответе нет ссылки "
                f"({json.dumps(data)[:160]})")
            continue
        if fetch(url, dst):
            got.append({"file": str(dst), "q": q, "src": "magnific_video_gen",
                        "kind": "video", "model": model})
            log(f"  clip {n:03d}: magnific/{model} — сгенерировано, «{q}»")
            n += 1
    if got:
        man_path.write_text(json.dumps(man + got, indent=1))


def fill_gaps(job, work: Path, total: float, model, key):
    """
    Добирает генерацией то, чего не нашлось в архивах и на стоках.

    Робот отбраковывает материал сам, и после отбраковки реального может не
    хватить на ролик. Раньше в этом случае MaterialMix просто уходил за
    заданную долю генерации и писал предупреждение — то есть дырку затыкал
    повтор одной и той же картинки по третьему разу.

    Теперь дырка закрывается новыми кадрами. Доля генерации при этом всё
    равно поднимается выше заказанной — но это честнее повтора: зритель
    видит разное, а не одно и то же трижды.

    ВИДЕО НЕ ГЕНЕРИРУЕТСЯ. Оно дорогое, и на этом канале не нужно: нехватку
    футажа закрывает фотография с движением камеры, которую от плавного
    стокового кадра на общем плане не отличить.
    """
    rej = vet.rejected_from(work)
    def usable(folder, pat, kind):
        out = 0
        for p in (work / folder).glob(pat):
            try:
                n = int(p.name.split("_")[1])
            except (IndexError, ValueError):
                continue
            if n not in set(rej.get(kind, [])):
                out += 1
        return out

    real = usable("archive", "arch_*", "arch") + usable("footage", "clip_*", "clip")
    have_gen = len(list((work / "images").glob("img_*")))

    # Сколько кадров в ролике и сколько из них под реальный материал.
    # Один файл спокойно показывается два-три раза разными кадрированиями,
    # поэтому нужное число файлов делится на 2.5.
    base = float((job.get("style_override") or {}).get(
        "base_duration_range", [4.2, 5.6])[0]) or 4.8
    share = float((job.get("style_override") or {}).get("generated_share", 0.30))
    shots = max(8, int(total / base))
    need_real = int(shots * (1 - share) / 2.5)

    # have_gen СЧИТАЛСЯ И НЕ ИСПОЛЬЗОВАЛСЯ — мёртвая переменная, ровно та
    # ошибка, от которой заведён smoke.py. Из-за неё добор смотрел только
    # на реальный материал: на прогоне ff-ep05 генерация дала ноль картинок,
    # реального материала было 45 при нужных 25, добор честно решил, что
    # всё в порядке, и молча вышел. Ноль генерации при заказанных 33%
    # экранного времени — это дырка на треть ролика, и заметить её здесь
    # было можно.
    need_gen = int(shots * share / 2.5) if share > 0 else 0
    log(f"  реального материала {real}, под ролик нужно около {need_real}; "
        f"генерации {have_gen}, нужно около {need_gen}")
    if share > 0 and have_gen == 0:
        raise SystemExit(
            f"генерации нет ни одной картинки при заказанных "
            f"{share*100:.0f}% экранного времени.\n"
            f"Это дырка на треть ролика, и закрывать её повтором архива "
            f"нельзя. Смотри выше, чем закончился этап изображений.")

    if real >= need_real and have_gen >= need_gen:
        return
    if real >= need_real:
        # реального хватает, не хватает именно генерации — добираем её
        missing = min(max(need_gen - have_gen, 0),
                      int(job.get("fill_limit", 24)))
        if not missing:
            return
        log(f"  ! генерации не хватает {need_gen - have_gen}; "
            f"догенерирую {missing} кадров")
        return _fill_generate(job, work, missing, model, key)
    missing = min(need_real - real, int(job.get("fill_limit", 24)))
    log(f"  ! не хватает {need_real - real}; догенерирую {missing} кадров")
    return _fill_generate(job, work, missing, model, key)


def _fill_generate(job, work: Path, missing: int, model, key):
    """
    Собственно добор генерацией. Вынесено из fill_gaps, потому что вызывать
    его нужно из двух мест: когда не хватает реального материала и когда не
    хватает самой генерации.
    """
    # Промпты добора: из спецификации, иначе строятся из архивных запросов —
    # они описывают ровно те предметы, которых не нашлось настоящими.
    base_prompts = job.get("fill_prompts") or [
        f"{q}, authentic looking, warm lamp light, aged materials, "
        f"shallow depth of field, photographic, cinematic, 16:9"
        for q in job.get("archive_queries", [])
    ]
    if not base_prompts:
        log("  ! нечем догенерировать: нет ни fill_prompts, ни archive_queries")
        return

    out = work / "images"
    out.mkdir(parents=True, exist_ok=True)
    got = 0
    # Нумерация с 900: добор не должен перебить основные промпты, которые
    # привязаны к порядку сценария номерами img_001..img_0NN.
    for k in range(missing):
        dst = out / f"img_{900 + k:03d}.jpg"
        if dst.exists():
            got += 1
            continue
        p = base_prompts[k % len(base_prompts)]
        r = requests.post(f"{XAI}/images/generations", timeout=180,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model, "prompt": p, "n": 1})
        if r.status_code != 200:
            log(f"  ! добор {k+1} не вышел: {r.status_code} {r.text[:140]}")
            continue
        try:
            url = r.json()["data"][0]["url"]
        except (KeyError, IndexError):
            log(f"  ! добор {k+1}: в ответе нет ссылки")
            continue
        dst.write_bytes(requests.get(url, timeout=120).content)
        got += 1
        log(f"  добор {k+1}/{missing}")
    # промпты добора кладутся рядом: build.py возьмёт из них слова для
    # смыслового подбора, иначе эти кадры лягут под текст случайно
    (out / "_fill_prompts.json").write_text(
        json.dumps([base_prompts[k % len(base_prompts)]
                    for k in range(missing)], ensure_ascii=False),
        encoding="utf-8")
    if got < missing:
        log(f"  ! добор дал {got} из {missing} — остальное закроют повторы")
    return got


def main(job_path, stage="all"):
    job = load_job(job_path)
    work = Path("work") / job["id"] / "assets"
    work.mkdir(parents=True, exist_ok=True)

    # Добор материала: озвучка и картинки уже есть, трогать их нельзя.
    # Только отбраковка, без скачивания. Нужен, чтобы перепроверить материал
    # после правки порогов или замены модели зрения: каждый прогон material
    # доливает новые файлы, и повторять его ради одной проверки — значит
    # раздувать кэш на гигабайт за раз.
    if stage == "vet":
        log("── отбраковка материала роботом (без скачивания)")
        vet.vet_all(job, work, use_vision=job.get("vet_vision", True))
        log("── что дал каждый запрос")
        yield_report(work, "footage", "clip")
        yield_report(work, "archive", "arch")
        return

    if stage == "material":
        fetch_material(job, work)
        log("── отбраковка материала роботом")
        vet.vet_all(job, work, use_vision=job.get("vet_vision", True))
        log("── что дал каждый запрос")
        yield_report(work, "footage", "clip")
        yield_report(work, "archive", "arch")

        # ДОЗАКАЧКА ПО ФАКТУ НЕХВАТКИ. Здесь известно и сколько годного
        # осталось, и сколько просит план — а значит, чинить нехватку надо
        # здесь, а не отправлять человека на второй круг stage: material
        # по предупреждению из rails.py.
        st = work / "state.json"
        if st.exists():
            total = json.loads(st.read_text()).get("total_audio", 0)
            if total:
                need = clips_needed(job, total)
                if top_up_footage(job, work, need):
                    log("── отбраковка дозакачанного")
                    vet.vet_all(job, work,
                                use_vision=job.get("vet_vision", True))
                    yield_report(work, "footage", "clip")
        else:
            log("  (длина ролика ещё не известна — дозакачка по факту "
                "нехватки будет на полном прогоне)")
        log("── материал добран, озвучка и картинки не тронуты")
        return

    log("── проверка ключей")
    check_keys()

    log("── озвучка")
    voice, marks, total = build_voice(job, work)

    log("── изображения")
    key = os.environ["XAI_API_KEY"].strip()
    magnific_key = (os.environ.get("MAGNIFIC_API_KEY") or "").strip()
    model = job.get("image_model", "grok-imagine-image")
    prompts = job["image_prompts"]

    # РАЗДЕЛЕНИЕ 70/30. Без ключа Magnific — всё как раньше, 100% на xAI:
    # это старый канал без подписки на Magnific, и поведение не должно
    # меняться только оттого, что функция теперь умеет больше.
    if magnific_key:
        share = float(job.get("magnific_image_share", MAGNIFIC_IMAGE_SHARE))
        p_magnific, p_xai = split_indexed(prompts, share)
        log(f"  разделение генерации: {len(p_magnific)} magnific / "
            f"{len(p_xai)} xai ({share*100:.0f}/{(1-share)*100:.0f})")
        images_magnific(p_magnific, work / "images", magnific_key)
    else:
        p_xai = list(enumerate(prompts, 1))

    if job.get("batch", True):
        # Пакет вдвое дешевле, но он же вдвое ненадёжнее: он может
        # закрыться пустым, протухнуть или потерять файл результатов.
        # Ронять на этом ВЕСЬ прогон нельзя — озвучка к этому моменту уже
        # сделана и уже оплачена, и потерять её из-за неудачного пакета
        # дороже, чем добрать картинки поштучно по полной цене.
        try:
            images_batch(p_xai, work / "images", model, key)
        except BatchFailed as e:
            if e.alive:
                # Пакет жив и будет выставлен в счёт. Уйти сейчас в
                # поштучную генерацию значит оплатить одни и те же
                # картинки дважды. Останавливаемся; состояние сохранено,
                # перезапуск подхватит пакет там же, где бросили.
                raise SystemExit(
                    f"{e}\n\nПоштучную догенерацию НЕ запускаю: пакет живой "
                    f"и всё равно будет оплачен, а поштучная стоила бы "
                    f"вдвое дороже за те же картинки.")
            log(f"  ! пакет не сложился: {e}")
            log("  перехожу на поштучную генерацию (полная цена вместо "
                "половинной) — иначе теряется уже оплаченная озвучка")
            images_sync(p_xai, work / "images", model, key)
    else:
        images_sync(p_xai, work / "images", model, key)

    # ПРОВЕРКА СРАЗУ, А НЕ НА МОНТАЖЕ. Без этой строки пустая папка
    # картинок доезжала до build.py, то есть до момента, когда уже
    # отработали отбраковка зрением и добор материала, а пустой результат
    # уехал в кэш. Падать надо там, где сломалось.
    made = len(list((work / "images").glob("img_*.jpg")))
    if not made:
        raise SystemExit(
            "генерация не дала ни одной картинки.\n"
            "Ни magnific, ни пакетом xAI, ни поштучно — значит, дело не в "
            "пакете, а в ключах (XAI_API_KEY" +
            (", MAGNIFIC_API_KEY" if magnific_key else "") +
            "), в модели (" + model + ") или в самих промптах: их мог "
            "отклонить фильтр содержания.\n"
            "Причины по каждому промпту напечатаны выше.")
    log(f"  картинок готово: {made} из {len(prompts)}")

    fetch_material(job, work)

    log("── отбраковка материала роботом")
    vet.vet_all(job, work, use_vision=job.get("vet_vision", True))

    log("── что дал каждый запрос")
    yield_report(work, "footage", "clip")
    yield_report(work, "archive", "arch")

    # ДОЗАКАЧКА ФУТАЖА ПО ФАКТУ НЕХВАТКИ — до добора генерацией, а не после.
    # Порядок важен: fill_gaps закрывает дырку РИСОВАННЫМИ кадрами, и если
    # пустить его первым, он честно закроет нехватку стока генерацией, доля
    # реального материала просядет, а настоящий футаж, который лежал в
    # двух запросах от нас, так и не будет скачан.
    need = clips_needed(job, total)
    if top_up_footage(job, work, need):
        log("── отбраковка дозакачанного")
        vet.vet_all(job, work, use_vision=job.get("vet_vision", True))
        yield_report(work, "footage", "clip")

    log("── добор генерацией того, чего не хватило")
    fill_gaps(job, work, total, model, key)

    (work / "state.json").write_text(json.dumps(
        {"total_audio": total, "marks": len(marks)}, indent=1))
    log(f"── готово. Звук {total/60:.1f} мин")


if __name__ == "__main__":
    # второй аргумент: material — добрать только футаж и архив,
    # без озвучки и генерации
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "all")
