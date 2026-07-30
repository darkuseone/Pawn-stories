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
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import vet

UA = {"User-Agent": "sleep-docs-pipeline/1.0 (educational video project)"}
TIMEOUT = 60

# Потолки на скачивание материала. Ролику нужны отрывки на 5-15 секунд,
# и ничего тяжелее сюда не требуется. Без потолков этап 1 однажды провисел
# 37 минут на одном файле с archive.org.
MAX_FILE_BYTES = 120 * 1024 * 1024      # 120 МБ на файл
FETCH_SECONDS = 90                      # столько ждём один файл
GATHER_BUDGET = 420                     # столько всего на один сбор


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


def images_sync(prompts, out: Path, model, key):
    """Быстрый режим: по одному запросу, полная цена, готово за минуты."""
    out.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(prompts, 1):
        dst = out / f"img_{i:03d}.jpg"
        if dst.exists():
            continue
        r = requests.post(f"{XAI}/images/generations", timeout=180,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model, "prompt": p, "n": 1})
        if r.status_code != 200:
            log(f"  ! картинка {i} не вышла: {r.status_code} {r.text[:160]}")
            continue
        url = r.json()["data"][0]["url"]
        dst.write_bytes(requests.get(url, timeout=120).content)
        log(f"  картинка {i}/{len(prompts)}")


def images_batch(prompts, out: Path, model, key, poll=120):
    """
    Дешёвый режим: пакет заданий, минус 50% от цены, до суток ожидания.
    Ссылки на готовые файлы живут около часа, поэтому качаем сразу
    как только пакет закрылся.
    """
    out.mkdir(parents=True, exist_ok=True)
    state = out / "_batch.json"

    if not state.exists():
        lines = [json.dumps({
            "custom_id": f"img_{i:03d}",
            "method": "POST",
            "url": "/v1/images/generations",
            "body": {"model": model, "prompt": p, "n": 1},
        }) for i, p in enumerate(prompts, 1)]
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

    while True:
        s = requests.get(f"{XAI}/batches/{bid}", timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {key}"}).json()
        pending = s.get("state", {}).get("num_pending", 0)
        if pending == 0:
            break
        log(f"  в очереди {pending}, жду {poll} сек")
        time.sleep(poll)

    ofid = s.get("output_file_id")
    body = requests.get(f"{XAI}/files/{ofid}/content", timeout=TIMEOUT,
                        headers={"Authorization": f"Bearer {key}"}).text
    n = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        cid = rec.get("custom_id", "")
        try:
            url = rec["response"]["body"]["data"][0]["url"]
        except Exception:
            log(f"  ! {cid} без результата")
            continue
        (out / f"{cid}.jpg").write_bytes(
            requests.get(url, timeout=120).content)
        n += 1
    log(f"  скачано {n} картинок")


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
    out, dropped = [], 0
    for v in r.json().get("videos", []):
        files = [f for f in v["video_files"]
                 if f.get("width", 0) >= 1280 and f.get("link")]
        if not files:
            continue
        # у Pexels нет поля тегов, но есть человекочитаемый адрес страницы
        # вида /video/antique-shop-interior-12345 — слова темы лежат в нём
        if not relevant(q, (v.get("url") or "").replace("-", " ")):
            dropped += 1
            continue
        best = sorted(files, key=lambda f: abs(f["width"] - 1920))[0]
        out.append({"url": best["link"], "src": "pexels",
                    "dur": v.get("duration", 0), "kind": "video"})
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
    out, dropped = [], 0
    for v in r.json().get("hits", []):
        vv = v.get("videos", {})
        link = (vv.get("large") or vv.get("medium") or {}).get("url")
        if not link:
            continue
        if not relevant(q, v.get("tags", "")):
            dropped += 1
            continue
        out.append({"url": link, "src": "pixabay",
                    "dur": v.get("duration", 0), "kind": "video"})
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


def src_archive_org(q, n):
    """Хроника. Фильтр по лицензии: только явное общественное достояние."""
    r = requests.get("https://archive.org/advancedsearch.php", timeout=TIMEOUT,
                     headers=UA,
                     params={"q": f'{q} AND mediatype:(movies) AND '
                                  f'licenseurl:(*publicdomain*)',
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
                             "gsrsearch": f"{q} filetype:video",
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
    "pexels": src_pexels,
    "pixabay": src_pixabay,
    "archive_org": src_archive_org,
    "wikimedia_video": src_wikimedia_video,
    "nasa": src_nasa,
    "met": src_met,
    "loc": src_loc,
    "wikimedia": src_wikimedia,
}

VIDEO_SOURCES = [src_pexels, src_pixabay, src_archive_org, src_wikimedia_video]
PHOTO_SOURCES = [src_met, src_loc, src_wikimedia]


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
    probe("PEXELS_API_KEY", "https://api.pexels.com/v1/search",
          {"Authorization": pex}, {"query": "test", "per_page": 1})
    probe("PIXABAY_API_KEY", "https://pixabay.com/api/",
          None, {"key": pix, "q": "test", "per_page": 3})

    if bad:
        raise SystemExit(
            "Сервисы не приняли ключи: " + ", ".join(bad) + ".\n"
            "Значения лежат в Settings -> Secrets and variables -> Actions.\n"
            "Чаще всего это устаревший ключ или значения, перепутанные местами\n"
            "между секретами. Ключ ElevenLabs начинается с sk_, voice_id — это\n"
            "короткий идентификатор голоса из Voice Library, а не ключ.")


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
    over = float(job.get("material_overshoot", 1.4))
    log("── футажи (" + ", ".join(f.__name__[4:] for f in vids) +
        f", запас x{over:g})")
    gather(job["footage_queries"], max(1, round(7 * over)), vids,
           work / "footage", "clip")
    log("── реальные фото из архивов (" +
        ", ".join(f.__name__[4:] for f in phot) + ")")
    gather(job["archive_queries"], max(1, round(4 * over)), phot,
           work / "archive", "arch")


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

    log(f"  реального материала {real}, под ролик нужно около {need_real}")
    if real >= need_real:
        return
    missing = min(need_real - real, int(job.get("fill_limit", 24)))
    log(f"  ! не хватает {need_real - real}; догенерирую {missing} кадров")

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
    # Нумерация с 900: добор не должен перебить основные промпты, которые
    # привязаны к порядку сценария номерами img_001..img_0NN.
    for k in range(missing):
        dst = out / f"img_{900 + k:03d}.jpg"
        if dst.exists():
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
        log(f"  добор {k+1}/{missing}")
    # промпты добора кладутся рядом: build.py возьмёт из них слова для
    # смыслового подбора, иначе эти кадры лягут под текст случайно
    (out / "_fill_prompts.json").write_text(
        json.dumps([base_prompts[k % len(base_prompts)]
                    for k in range(missing)], ensure_ascii=False),
        encoding="utf-8")


def main(job_path, stage="all"):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
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
        return

    if stage == "material":
        fetch_material(job, work)
        log("── отбраковка материала роботом")
        vet.vet_all(job, work, use_vision=job.get("vet_vision", True))
        log("── материал добран, озвучка и картинки не тронуты")
        return

    log("── проверка ключей")
    check_keys()

    log("── озвучка")
    voice, marks, total = build_voice(job, work)

    log("── изображения")
    key = os.environ["XAI_API_KEY"].strip()
    model = job.get("image_model", "grok-imagine-image")
    prompts = job["image_prompts"]
    if job.get("batch", True):
        images_batch(prompts, work / "images", model, key)
    else:
        images_sync(prompts, work / "images", model, key)

    fetch_material(job, work)

    log("── отбраковка материала роботом")
    vet.vet_all(job, work, use_vision=job.get("vet_vision", True))

    log("── добор генерацией того, чего не хватило")
    fill_gaps(job, work, total, model, key)

    (work / "state.json").write_text(json.dumps(
        {"total_audio": total, "marks": len(marks)}, indent=1))
    log(f"── готово. Звук {total/60:.1f} мин")


if __name__ == "__main__":
    # второй аргумент: material — добрать только футаж и архив,
    # без озвучки и генерации
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "all")
