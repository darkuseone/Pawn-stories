"""
scout.py — поиск материала ГЛАЗАМИ: сцены -> кандидаты -> листы -> закрепление.

    python pipeline/scout.py outline jobs/<id>.json      # фразы сценария
    python pipeline/scout.py search  jobs/<id>.json [s03 s07 ...]
    python pipeline/scout.py pull    jobs/<id>.json      # листы из релиза scout-<job>
    python pipeline/scout.py pin     jobs/<id>.json s03-02 s07-01@12.5 ...
    python pipeline/scout.py check   jobs/<id>.json      # ссылки, 1080p, покрытие
    python pipeline/scout.py audit   jobs/<id>.json [N]  # готовый ролик глазами

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНСТРУМЕНТ. Правило канала — материал выбирает модель в
чате, глазами, и закрепляет прямыми ссылками ДО пуша (CLAUDE.md, «РАЗДЕЛЕНИЕ
РОЛЕЙ»). Инструмента под это не было: на каждый ролик поиск, нарезка кадров
и листы писались заново разовыми скриптами, и закреплённое в итоге
привязывалось к сценарию только словами q — то есть ложилось «где-то рядом
по теме», а не под фразу, ради которой его выбирали.

КАК УСТРОЕНО.

1. В спецификации — поле `scenes`: кусок сценария (цитата `at`), что должно
   быть в кадре (`want`) и 2-4 запроса (`queries`). Список фраз с номерами
   печатает `outline`.
2. `search` идёт по стокам и открытым архивам, берёт по каждому кандидату
   ТРИ кадра (начало, середина, конец — больше не надо, это токены) и
   собирает на сцену один контактный лист `work/<id>/scout/sheet_<сцена>.jpg`
   с номерами кандидатов. Сами видео НЕ качаются: у Pexels кадры берутся из
   его превью, у остальных ffmpeg читает три кадра прямо по ссылке.
3. Листы СМОТРИТ МОДЕЛЬ и выбирает номера. `pin s03-02@12.5` переносит
   кандидата в `pinned_footage` / `pinned_archive` с цитатой сцены в `at` и
   точкой входа `t0` — монтаж поставит файл ровно под эту фразу
   (build.pinned_bindings) и начнёт клип с выбранной секунды.
4. `check` — перед пушем: каждая ссылка живая, видео не больше 1080p,
   цитаты находятся в сценарии, сколько сцен закрыто и сколько клипов по
   20+ секунд (без них выдохи из pacing.py уходят фотографиям).

КЛЮЧИ. Pexels и Pixabay без ключа не отвечают, а ключи лежат только в
секретах Actions. Поэтому `search` запускается там же стадией `scout`
(build.yml): Actions ищет и собирает листы — выбирать он не умеет и не
должен, — и выкладывает их в релиз `scout-<job>`. Репозиторий публичный,
`pull` забирает листы и candidates.json оттуда без токена. Есть ключи в
окружении — `search` работает и локально, тем же кодом.

Wikimedia Commons ключа не требует и ищется всегда: хроника, техника,
вещи середины века — то, чего нет на стоках.
"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests

from jobspec import load_job, find_at, at_list, norm_text

ROOT = Path(__file__).parent.parent
UA = {"User-Agent": "pawn-stories-scout/1.0 (educational video project)"}
TIMEOUT = 30
REPO = "darkuseone/Pawn-stories"

# Видео: минимум 1280 по ширине, максимум 1920. 4K не берём — на 1080p-
# выходе не видно, а кэш и время монтажа раздувает (ПАЙПЛАЙН.md, шаг 3).
MIN_W, MAX_W = 1280, 1920
# Клип короче этого бесполезен даже во вступлении.
MIN_SEC = 5
# Клип длиннее этого качается минуту ради куска: берём, но помечаем.
LONG_SEC = 60
PER_QUERY = 4
MAX_PER_SCENE = 12

THUMB_W, THUMB_H = 320, 180
LABEL_H = 26


def log(*a):
    print(*a, flush=True)


def work_dir(job) -> Path:
    return ROOT / "work" / job["id"] / "scout"


# ───────────────────────── источники ─────────────────────────
# Каждый возвращает кандидатов одного вида:
#   {src, page, url, preview, thumbs, w, h, dur, kind, title}
# url — что закрепляем; preview — лёгкая версия для кадров; thumbs — готовые
# картинки-кадры, если источник их отдаёт (тогда ffmpeg не нужен).

def _get(url, tries=4, **kw):
    # 429 у открытых API — не отказ, а «подожди». Три повтора с паузой по
    # Retry-After; Commons зовёт с tries=1 — см. _commons.
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            log(f"    ! {url[:60]}: {e}")
            return None
        if r.status_code != 429 or attempt == tries - 1:
            break
        try:
            wait = min(30.0, float(r.headers.get("Retry-After") or 0))
        except ValueError:
            wait = 0.0
        time.sleep(max(wait, 3.0 * (attempt + 1)))
    if r.status_code != 200:
        log(f"    ! {url[:60]}: ответ {r.status_code} {r.text[:100]}")
        return None
    return r


def pexels_video(q, n):
    k = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not k:
        return []
    r = _get("https://api.pexels.com/videos/search",
             headers={"Authorization": k},
             params={"query": q, "per_page": n, "orientation": "landscape"})
    out = []
    for v in (r.json().get("videos", []) if r else []):
        files = [f for f in v.get("video_files", []) if f.get("link")]
        good = [f for f in files if MIN_W <= (f.get("width") or 0) <= MAX_W]
        if not good:
            continue
        best = min(good, key=lambda f: abs(f["width"] - 1920))
        small = min(files, key=lambda f: f.get("width") or 99999)
        pics = [p.get("picture") for p in v.get("video_pictures", [])
                if p.get("picture")]
        thumbs = ([pics[0], pics[len(pics) // 2], pics[-1]]
                  if len(pics) >= 3 else [])
        out.append(dict(src="pexels", page=v.get("url", ""), url=best["link"],
                        preview=small["link"], thumbs=thumbs,
                        w=best["width"], h=best.get("height", 0),
                        dur=int(v.get("duration") or 0), kind="video",
                        title=(v.get("url") or "").rstrip("/").split("/")[-1]))
    return out


def pixabay_video(q, n):
    k = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not k:
        return []
    r = _get("https://pixabay.com/api/videos/",
             params={"key": k, "q": q, "per_page": max(n, 3)})
    out = []
    for v in (r.json().get("hits", []) if r else [])[:n]:
        vv = v.get("videos", {})
        pick = None
        for name in ("large", "medium"):
            f = vv.get(name) or {}
            if f.get("url") and MIN_W <= (f.get("width") or 0) <= MAX_W:
                pick = f
                break
        # Вертикальное в кадр 16:9 не встанет. Фильтра по тегам здесь НЕТ
        # сознательно: проверен на живой выдаче и выкидывал годное
        # (смартфон под «phone calls»), оставляя чужое (воробей под «house
        # keys») — теги Pixabay не говорят, что в кадре. Судья — зрение.
        if not pick or (pick.get("height") or 0) > (pick.get("width") or 0):
            continue
        small = vv.get("tiny") or vv.get("small") or pick
        out.append(dict(src="pixabay", page=v.get("pageURL", ""),
                        url=pick["url"], preview=small.get("url") or pick["url"],
                        thumbs=[], w=pick.get("width", 0),
                        h=pick.get("height", 0), dur=int(v.get("duration") or 0),
                        kind="video", title=v.get("tags", "")))
    return out


def pexels_photo(q, n):
    k = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not k:
        return []
    r = _get("https://api.pexels.com/v1/search", headers={"Authorization": k},
             params={"query": q, "per_page": n, "orientation": "landscape"})
    out = []
    for p in (r.json().get("photos", []) if r else []):
        srcs = p.get("src") or {}
        orig = srcs.get("original")
        if not orig:
            continue
        # original может быть 6000 px и 20 МБ — просим CDN ужать до 2400
        url = orig + "?auto=compress&cs=tinysrgb&w=2400"
        out.append(dict(src="pexels", page=p.get("url", ""), url=url,
                        preview=srcs.get("medium") or url,
                        thumbs=[srcs.get("medium") or url], w=p.get("width", 0),
                        h=p.get("height", 0), dur=0, kind="image",
                        title=p.get("alt", "")))
    return out


def pixabay_photo(q, n):
    k = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not k:
        return []
    r = _get("https://pixabay.com/api/",
             params={"key": k, "q": q, "per_page": max(n, 3),
                     "image_type": "photo", "orientation": "horizontal"})
    out = []
    for p in (r.json().get("hits", []) if r else [])[:n]:
        url = p.get("largeImageURL")
        if not url:
            continue
        out.append(dict(src="pixabay", page=p.get("pageURL", ""), url=url,
                        preview=p.get("webformatURL") or url,
                        thumbs=[p.get("webformatURL") or url],
                        w=p.get("imageWidth", 0), h=p.get("imageHeight", 0),
                        dur=0, kind="image", title=p.get("tags", "")))
    return out


_COMMONS_DOWN = [False]


def _commons(q, n, video: bool):
    """Commons: только общественное достояние и CC0, атрибуцию не берём.

    Первый же отказ (429 на общем адресе) выключает Commons до конца
    прогона: повторы с паузой на каждый запрос стоили семи минут поиска
    при нуле кандидатов с него.
    """
    if _COMMONS_DOWN[0]:
        return []
    r = _get("https://commons.wikimedia.org/w/api.php", headers=UA, tries=1,
             params={"action": "query", "generator": "search",
                     "gsrsearch": f"{q} filetype:{'video' if video else 'bitmap'}",
                     "gsrnamespace": 6, "gsrlimit": n * 3,
                     "prop": "imageinfo",
                     "iiprop": "url|size|extmetadata|mediatype",
                     "iiurlwidth": 640, "format": "json"})
    if r is None:
        _COMMONS_DOWN[0] = True
        log("    ! Commons не отвечает — до конца прогона ищу без него")
        return []
    out = []
    pages = (r.json().get("query", {}).get("pages", {}) if r else {}) or {}
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        ii = (page.get("imageinfo") or [{}])[0]
        lic = ((ii.get("extmetadata") or {}).get("LicenseShortName", {})
               .get("value", "")).lower()
        if not any(t in lic for t in ("public domain", "cc0", "pd-")):
            continue
        url = ii.get("url") or ""
        if video:
            if not url.lower().endswith((".webm", ".ogv", ".mp4")):
                continue
            if int(ii.get("size") or 0) > 120 * 1024 * 1024:
                continue
            thumbs = []
        else:
            if (ii.get("width") or 0) < 1200:
                continue
            thumbs = [ii.get("thumburl") or url]
        out.append(dict(src="wikimedia", page=ii.get("descriptionurl", ""),
                        url=url, preview=url, thumbs=thumbs,
                        w=ii.get("width", 0), h=ii.get("height", 0),
                        dur=int(float(ii.get("duration") or 0)),
                        kind="video" if video else "image",
                        title=page.get("title", "")))
        if len(out) >= n:
            break
    return out


def commons_video(q, n):
    return _commons(q, n, True)


def commons_photo(q, n):
    return _commons(q, n, False)


VIDEO = [pexels_video, pixabay_video, commons_video]
PHOTO = [pexels_photo, pixabay_photo, commons_photo]


# ───────────────────────── кадры и листы ─────────────────────────

def _probe(url):
    """Длина и размер видео по ссылке — ffprobe читает только заголовок."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", url], capture_output=True, text=True, timeout=90)
    try:
        d = json.loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        return (float((d.get("format") or {}).get("duration") or 0),
                int(st.get("width") or 0), int(st.get("height") or 0))
    except (ValueError, TypeError):
        return 0.0, 0, 0


def _grab(url, sec, dst: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{sec:.2f}", "-i", url,
         "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", str(dst)],
        capture_output=True, timeout=120)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 500


def _download(url, dst: Path) -> bool:
    r = _get(url, headers=UA)
    if not r:
        return False
    dst.write_bytes(r.content)
    return dst.stat().st_size > 500


def frames_for(c, folder: Path, cid: str):
    """
    Три кадра видео (или один кадр фото) на диск. Возвращает пути.

    Кадры видео — 10%, 50%, 90% длины: начало и конец самого файла часто
    заставка и затемнение, по ним клип не оценить.
    """
    folder.mkdir(parents=True, exist_ok=True)
    # Кэш кадров — по ССЫЛКЕ, а не по номеру кандидата. Номера s01-01…
    # при новом поиске выдаются заново, и кэш по номеру подставлял под
    # подпись нового кандидата кадры старого: лист врал ровно там, где на
    # нём принимается решение.
    import hashlib
    key = hashlib.sha1(c["url"].encode()).hexdigest()[:12]
    out = []
    if c["thumbs"]:
        for k, u in enumerate(c["thumbs"]):
            p = folder / f"{key}_{k}.jpg"
            if p.exists() or _download(u, p):
                out.append(p)
        return out
    if c["kind"] != "video":
        return out
    if not c["dur"]:
        dur, w, h = _probe(c["url"])
        c["dur"] = int(dur)
        c["w"], c["h"] = c["w"] or w, c["h"] or h
    dur = c["dur"] or 10
    for k, frac in enumerate((0.1, 0.5, 0.9)):
        p = folder / f"{key}_{k}.jpg"
        if p.exists() or _grab(c["preview"], dur * frac, p):
            out.append(p)
    return out


def _font(size):
    from PIL import ImageFont
    for p in (ROOT / "assets" / "fonts" / "Oswald-Bold.ttf",
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def sheet(scene, cands, frames, dst: Path):
    """
    Контактный лист сцены. Видео — строка на кандидата: три кадра и
    подпись. Фото — по три в ряд, подпись под каждым: целая строка на
    одну фотографию — это лишние токены при просмотре, а не информация.

    Подпись несёт всё, что нужно для решения без открытия ссылки: номер
    для pin, источник, длину и размер. Длина — главная цифра: клип короче
    слота под фразу не встанет под неё вовсе.
    """
    from PIL import Image, ImageDraw
    vids = [c for c in cands if frames.get(c["id"]) and c["kind"] == "video"]
    pics = [c for c in cands if frames.get(c["id"]) and c["kind"] != "video"]
    if not vids and not pics:
        return None
    head, W = 44, THUMB_W * 3
    rows = len(vids) + (len(pics) + 2) // 3
    im = Image.new("RGB", (W, head + rows * (THUMB_H + LABEL_H)), (18, 18, 18))
    d = ImageDraw.Draw(im)
    d.text((8, 6), f"{scene['id']}: {scene.get('want', '')}"[:90],
           fill=(255, 220, 120), font=_font(22))
    small = _font(17)

    def tile(path, x, y):
        try:
            t = Image.open(path).convert("RGB")
        except OSError:
            return
        t.thumbnail((THUMB_W, THUMB_H))
        im.paste(t, (x + (THUMB_W - t.width) // 2, y + (THUMB_H - t.height) // 2))

    y = head
    for c in vids:
        for k, p in enumerate(frames[c["id"]][:3]):
            tile(p, k * THUMB_W, y)
        size = f"{c['w']}x{c['h']}" if c.get("w") else "?"
        warn = "  ДЛИННЫЙ" if c["dur"] > LONG_SEC else ""
        d.rectangle((0, y + THUMB_H, W, y + THUMB_H + LABEL_H), fill=(0, 0, 0))
        d.text((8, y + THUMB_H + 2),
               f"{c['id']}   {c['src']}   {c['dur']}s   {size}{warn}   "
               f"{c.get('title', '')[:40]}", fill=(255, 255, 255), font=small)
        y += THUMB_H + LABEL_H
    for n in range(0, len(pics), 3):
        d.rectangle((0, y + THUMB_H, W, y + THUMB_H + LABEL_H), fill=(0, 0, 0))
        for k, c in enumerate(pics[n:n + 3]):
            tile(frames[c["id"]][0], k * THUMB_W, y)
            size = f"{c['w']}x{c['h']}" if c.get("w") else "?"
            d.text((k * THUMB_W + 6, y + THUMB_H + 2),
                   f"{c['id']} {c['src']} {size}", fill=(255, 255, 255),
                   font=small)
        y += THUMB_H + LABEL_H
    im.save(dst, quality=82)
    return dst


# ───────────────────────── команды ─────────────────────────

def sentences(job):
    """Фразы сценария с номерами блока — для outline и check."""
    out = []
    for b, text in enumerate(job.get("script_blocks") or []):
        for s in re.split(r"(?<=[.!?])\s+", str(text).strip()):
            if s:
                out.append((b, s))
    return out


def cmd_outline(job, _args):
    """Фразы по блокам с номерами: из них берутся цитаты at для сцен."""
    have = {norm_text(a) for sc in job.get("scenes") or [] for a in at_list(sc)}
    for n, (b, s) in enumerate(sentences(job)):
        mark = "*" if any(h and h in norm_text(s) for h in have) else " "
        print(f"{mark} b{b} #{n:03d}  {s}")


def search_scene(sc, job):
    kind = sc.get("kind", "video")
    n = int(sc.get("n") or PER_QUERY)
    funcs = (VIDEO if kind == "video" else PHOTO if kind == "image"
             else VIDEO + PHOTO)
    seen, cands = set(), []
    for q in sc["queries"]:
        for f in funcs:
            for c in f(q, n):
                if c["url"] in seen:
                    continue
                if c["kind"] == "video" and c["dur"] and c["dur"] < MIN_SEC:
                    continue
                seen.add(c["url"])
                c["q"] = q
                cands.append(c)
        time.sleep(0.3)                    # вежливость к открытым API
    # Длинные клипы — вперёд: под фразу нужен файл, закрывающий слот
    # целиком, а короткий на листе — лишь на вступление.
    cands.sort(key=lambda c: (c["kind"] != "video", -(min(c["dur"], 40))))
    cands = cands[:MAX_PER_SCENE]
    for k, c in enumerate(cands, 1):
        c["id"] = f"{sc['id']}-{k:02d}"
    return cands


def cmd_search(job, args):
    scenes = job.get("scenes") or []
    if not scenes:
        raise SystemExit("в спецификации нет scenes — сначала опиши сцены "
                         "(см. шапку scout.py и ПАЙПЛАЙН.md, шаг 3)")
    if args:
        scenes = [s for s in scenes if s["id"] in set(args)]
    if not (os.environ.get("PEXELS_API_KEY") or os.environ.get("PIXABAY_API_KEY")):
        log("! ключей Pexels/Pixabay нет — ищу только по Commons. Полный "
            "поиск: стадия scout в Actions (ключи в секретах)")
    out = work_dir(job)
    frames_dir = out / "frames"
    out.mkdir(parents=True, exist_ok=True)
    cand_file = out / "candidates.json"
    allc = json.loads(cand_file.read_text()) if cand_file.exists() else {}
    for sc in scenes:
        log(f"── {sc['id']}: {sc.get('want', '')}")
        cands = search_scene(sc, job)
        with ThreadPoolExecutor(max_workers=6) as ex:
            got = list(ex.map(lambda c: (c["id"], frames_for(c, frames_dir, c["id"])),
                              cands))
        frames = {cid: fr for cid, fr in got if fr}
        cands = [c for c in cands if c["id"] in frames]
        p = sheet(sc, cands, frames, out / f"sheet_{sc['id']}.jpg")
        allc[sc["id"]] = [{k: c[k] for k in ("id", "src", "page", "url", "w",
                                             "h", "dur", "kind", "q", "title")}
                          for c in cands]
        by_src = {}
        for c in cands:
            by_src[c["src"]] = by_src.get(c["src"], 0) + 1
        log(f"   кандидатов {len(cands)} "
            + " ".join(f"{k}:{v}" for k, v in by_src.items())
            + (f" -> {p.relative_to(ROOT)}" if p else " — ПУСТО, перепиши queries"))
        cand_file.write_text(json.dumps(allc, ensure_ascii=False, indent=1))
    log(f"кандидаты: {cand_file.relative_to(ROOT)}")


def cmd_pull(job, args):
    """Листы и candidates.json из релиза scout-<job> (публичный репозиторий)."""
    tag = f"scout-{Path(sys.argv[2]).stem}"
    r = _get(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    if not r:
        raise SystemExit(f"релиза {tag} нет — запусти стадию scout в Actions")
    out = work_dir(job)
    out.mkdir(parents=True, exist_ok=True)
    for a in r.json().get("assets", []):
        dst = out / a["name"]
        rr = _get(a["browser_download_url"], headers=UA)
        if rr:
            dst.write_bytes(rr.content)
            log(f"  {dst.relative_to(ROOT)}")


def _save_spec(path: Path, spec: dict):
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


def cmd_pin(job, args):
    """
    s03-02 или s03-02@12.5 — кандидат в закреплённый материал.

    Пишется в СЫРОЙ файл спецификации (без умолчаний канала), с цитатой
    сцены в at, want сцены в q и точкой входа в t0. Повторный pin той же
    ссылки не дублирует запись, а обновляет at и t0.
    """
    path = Path(sys.argv[2])
    spec = json.loads(path.read_text(encoding="utf-8"))
    cand_file = work_dir(job) / "candidates.json"
    if not cand_file.exists():
        raise SystemExit(f"нет {cand_file} — сначала search или pull")
    allc = json.loads(cand_file.read_text())
    by_id = {c["id"]: (sid, c) for sid, cs in allc.items() for c in cs}
    scenes = {s["id"]: s for s in spec.get("scenes") or []}
    for arg in args:
        cid, _, t0 = arg.partition("@")
        if cid not in by_id:
            raise SystemExit(f"{cid}: нет такого кандидата в {cand_file.name}")
        sid, c = by_id[cid]
        sc = scenes.get(sid, {})
        field = "pinned_footage" if c["kind"] == "video" else "pinned_archive"
        rec = {"url": c["url"],
               "q": sc.get("want") or c.get("q", ""),
               "note": f"{cid} {c['src']} {c['dur']}s {c['w']}x{c['h']} {c['page']}"}
        if sc.get("at"):
            rec["at"] = sc["at"]
        if t0:
            rec["t0"] = float(t0)
        items = spec.setdefault(field, [])
        for it in items:
            if it.get("url") == rec["url"]:
                it.update(rec)
                break
        else:
            items.append(rec)
        log(f"  {cid} -> {field}" + (f" с {t0} с" if t0 else "")
            + (f"  под «{str(sc.get('at'))[:50]}»" if sc.get("at") else ""))
    _save_spec(path, spec)


def _alive(it):
    """Ссылка отдаёт файл? Для видео заодно размер и длина."""
    url = it["url"]
    try:
        r = requests.get(url, headers={**UA, "Range": "bytes=0-1023"},
                         timeout=TIMEOUT, stream=True)
        code = r.status_code
        ctype = r.headers.get("Content-Type", "")
        r.close()
    except requests.RequestException as e:
        return dict(it=it, ok=False, why=str(e)[:80])
    if code not in (200, 206):
        return dict(it=it, ok=False, why=f"код {code}")
    if "text/html" in ctype:
        return dict(it=it, ok=False, why="это страница, а не файл")
    res = dict(it=it, ok=True, why="")
    if (it.get("kind") or it["_kind"]) == "video":
        res["dur"], res["w"], res["h"] = _probe(url)
    return res


def cmd_check(job, _args):
    """
    Перед пушем. Роняет (код 1) на мёртвой ссылке и на цитате, которой нет
    в сценарии; остальное — заметки.
    """
    items = ([{**it, "_kind": "video"} for it in job.get("pinned_footage") or []]
             + [{**it, "_kind": "image"} for it in job.get("pinned_archive") or []])
    if not items:
        raise SystemExit("закреплённого материала нет")
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(_alive, items))
    bad = [r for r in res if not r["ok"]]
    vids = [r for r in res if r["ok"] and "dur" in r]
    big = [r for r in vids if r["w"] > MAX_W]
    small = [r for r in vids if 0 < r["w"] < MIN_W]
    long_ok = [r for r in vids if r["dur"] - float(r["it"].get("t0") or 0) >= 20]
    script = [str(b) for b in job.get("script_blocks") or []]
    no_at = [it for it in items if not at_list(it)]
    lost_at = [(it, a) for it in items for a in at_list(it)
               if find_at(script, a) is None]

    print(f"закреплено: видео {sum(1 for i in items if i['_kind'] == 'video')}, "
          f"фото {sum(1 for i in items if i['_kind'] == 'image')}")
    print(f"  живых ссылок {len(res) - len(bad)} из {len(res)}")
    for r in bad:
        print(f"  ! МЁРТВАЯ: {r['it']['url'][:90]} — {r['why']}")
    print(f"  клипов 20+ с (от t0): {len(long_ok)} из {len(vids)}"
          + ("   ! мало: выдохи до 27 с уйдут фотографиям" if len(long_ok) < 5 else ""))
    for r in big:
        print(f"  ! больше 1080p: {r['w']}x{r['h']} {r['it']['url'][:70]}")
    for r in small:
        print(f"  ! меньше 720p: {r['w']}x{r['h']} {r['it']['url'][:70]}")
    print(f"  привязаны к фразе (at): {len(items) - len(no_at)} из {len(items)}")
    for it, a in lost_at:
        print(f"  ! цитаты «{a[:60]}» нет в сценарии")

    scenes = job.get("scenes") or []
    if scenes:
        pinned_at = {norm_text(a) for it in items for a in at_list(it)}
        empty = [s["id"] for s in scenes
                 if not any(norm_text(a) in pinned_at for a in at_list(s))]
        print(f"  сцен закрыто: {len(scenes) - len(empty)} из {len(scenes)}"
              + (f"   пустые: {', '.join(empty)}" if empty else ""))

    sents = sentences(job)
    covered = sum(1 for _b, s in sents
                  if any(norm_text(a) in norm_text(s) or norm_text(s) in norm_text(a)
                         for it in items for a in at_list(it)))
    print(f"  фраз сценария под привязанным кадром: {covered} из {len(sents)}")
    if bad or lost_at:
        raise SystemExit(1)


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def cmd_audit(job, args):
    """
    ГОТОВЫЙ РОЛИК ГЛАЗАМИ: кадр посреди фразы + сама фраза, на листах.

    Обязательная проверка после сборки (CLAUDE.md, «ГОТОВЫЙ РОЛИК
    ПРОВЕРЯЕТСЯ ГЛАЗАМИ»): соответствует ли картинка тексту, который в эту
    секунду звучит. Ролик НЕ качается целиком — ffmpeg читает по ссылке
    релиза только нужные кадры. Берётся исходник шортсов (без субтитров,
    они закрывали бы низ кадра), нет его — final.mp4. Шкала — та же
    marks_final.json из релиза, по ней резались и субтитры.

    N — сколько фраз (умолчание 24), равномерно по ролику. Листы по 12
    кадров: work/<id>/scout/audit_N.jpg.
    """
    from PIL import Image, ImageDraw
    n = int(args[0]) if args else 24
    tag = f"final-{Path(sys.argv[2]).stem}"
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    r = _get(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    if not r:
        raise SystemExit(f"релиза {tag} нет — ролик ещё не собран")
    names = {a["name"] for a in r.json().get("assets", [])}
    video = next((v for v in ("_shorts-source.mp4", "clean.mp4", "final.mp4")
                  if v in names), None)
    if not video or "marks_final.json" not in names:
        raise SystemExit(f"в {tag} нет видео или marks_final.json")
    marks = _get(f"{base}/marks_final.json").json()
    step = max(1, len(marks) // n)
    pick = [(i, m) for i, m in enumerate(marks)][::step][:n]
    out = work_dir(job) / "audit"
    out.mkdir(parents=True, exist_ok=True)

    def grab(item):
        i, m = item
        t = (m["start"] + m["end"]) / 2
        p = out / f"f_{i:03d}.jpg"
        ok = p.exists() or _grab(f"{base}/{video}", t, p)
        return i, m, t, (p if ok else None)

    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(grab, pick))
    tw, th, lines = THUMB_W + 160, (THUMB_W + 160) * 9 // 16, 4
    cell_h = th + lines * 20 + 10
    small = _font(16)
    rows = []
    for k in range(0, len(got), 12):
        part = got[k:k + 12]
        im = Image.new("RGB", (tw * 3, cell_h * ((len(part) + 2) // 3)), (18, 18, 18))
        d = ImageDraw.Draw(im)
        for c, (i, m, t, p) in enumerate(part):
            x, y = (c % 3) * tw, (c // 3) * cell_h
            if p:
                fr = Image.open(p).convert("RGB")
                fr = fr.resize((tw - 4, th - 4))
                im.paste(fr, (x + 2, y + 2))
            label = f"#{i} {int(t // 60)}:{int(t % 60):02d}  " + m["text"]
            for L, line in enumerate(_wrap(label, 58)[:lines]):
                d.text((x + 6, y + th + 2 + L * 20), line,
                       fill=(255, 230, 150) if L == 0 else (235, 235, 235),
                       font=small)
        dst = work_dir(job) / f"audit_{k // 12 + 1}.jpg"
        im.save(dst, quality=85)
        rows.append(dst)
    (work_dir(job) / "audit.json").write_text(json.dumps(
        [{"i": i, "t": round(t, 1), "text": m["text"]} for i, m, t, _p in got],
        ensure_ascii=False, indent=1))
    log(f"кадров {len(got)} из {len(marks)} фраз ({video}); листы:")
    for p in rows:
        log(f"  {p.relative_to(ROOT)}")


COMMANDS = {"outline": cmd_outline, "search": cmd_search, "pull": cmd_pull,
            "pin": cmd_pin, "check": cmd_check, "audit": cmd_audit}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        raise SystemExit(__doc__)
    job = load_job(sys.argv[2])
    COMMANDS[sys.argv[1]](job, sys.argv[3:])


if __name__ == "__main__":
    main()
