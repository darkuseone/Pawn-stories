"""
smoke.py — прогон конвейера на настоящих файлах без ключей и без денег.

    python pipeline/smoke.py jobs/pawn-02.json

Зачем отдельный файл: py_compile ловит только синтаксис. Две ошибки подряд
уехали в боевой прогон именно потому, что модуль компилировался, но никогда
не запускался — сначала неверное имя модели зрения, потом ссылка на
удалённую константу (NameError). Обе видны за секунду, если просто вызвать
функции.

Здесь вызываются все шаги, которые можно вызвать бесплатно:
  vet.vet_all      — отбраковка (зрение выключается снятием ключа)
  build.plan_shots — раскладка кадров по таймлайну
  channel.check    — проверка темы на повтор
  youtube.norm     — поиск глав в сценарии

Рендера здесь нет: он долгий, а ломается не он. Прогонять ПЕРЕД каждым
пушем в main.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jobspec import load_job


def main(job_path):
    job = load_job(job_path)
    work = Path("work") / job["id"] / "assets"
    if not work.exists():
        raise SystemExit(
            f"нет {work} — сначала синтетика: python pipeline/mock.py {job_path}")

    os.environ.pop("XAI_API_KEY", None)      # зрение не трогаем, оно платное
    import vet, build, channel, youtube, style as style_mod

    print("── vet")
    vet.vet_all(job, work, use_vision=True)
    rej = vet.rejected_from(work)
    print(f"   отбраковано: {({k: len(v) for k, v in rej.items()})}")

    print("── локальный отбор (титры и дубли)")
    from PIL import Image, ImageDraw, ImageFont
    card = Image.new("RGB", (1280, 720), (8, 8, 8))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(card).text((140, 280), "Internet Archive",
                              font=font, fill=(240, 240, 240))
    problems = vet.content_problems([card], job)
    if not problems:
        raise SystemExit("титр Internet Archive не поймался локально")
    print(f"   титр пойман: {problems[0]}")
    h1 = vet.dhash(card)
    h2 = vet.dhash(card.copy())
    if vet.hamming(h1, h2) != 0:
        raise SystemExit("dHash не сходится на копии кадра")
    import tempfile
    td = Path(tempfile.mkdtemp())
    a = td / "clip_001_pexels.jpg"
    b = td / "clip_002_archive.org.jpg"
    card.save(a)
    card.save(b)
    decided = {}
    n_dup = vet.drop_visual_dupes(
        [a, b], {a: vet.dhash(Image.open(a)), b: vet.dhash(Image.open(b))},
        {a: "pexels", b: "archive.org"}, decided)
    if n_dup != 1 or decided.get(b, (True, ""))[0] is not False:
        raise SystemExit(f"дубль не отсеялся: {n_dup} {decided}")
    print("   дубль archive.org vs pexels отсеян")

    print("── темы")
    print(f"   {channel.check(job) or 'не повтор'}")

    print("── главы")
    seen = set()
    for i, b in enumerate(job["script_blocks"], 1):
        k = youtube.norm(b.strip().split(".")[0])[:45]
        if not k:
            raise SystemExit(f"глава {i}: пустой ключ поиска")
        if k in seen:
            raise SystemExit(f"глава {i}: начало блока не уникально")
        seen.add(k)
    n_ch = len(job.get("youtube", {}).get("chapters", []))
    if n_ch != len(job["script_blocks"]):
        raise SystemExit(f"глав {n_ch}, блоков {len(job['script_blocks'])}")
    if n_ch < 3:
        raise SystemExit(f"глав {n_ch} — YouTube показывает их от трёх")
    print(f"   {n_ch} глав, начала уникальны")

    # ТИПЫ СПИСОЧНЫХ ПОЛЕЙ. Спецификации пишутся в чате, и строка вместо
    # списка глазами не видна: в файле лежит "тег один, тег два" и выглядит
    # совершенно нормально. Дальше join разбирает её ПОСИМВОЛЬНО.
    # На ff-ep05 это дало 341 «тег» по одной букве и падение youtube.py на
    # лимите тегов — на самом последнем шаге, уже после полного монтажа.
    print("── типы полей")
    for path, val in (("youtube.tags", job.get("youtube", {}).get("tags")),
                      ("youtube.hashtags", job.get("youtube", {}).get("hashtags")),
                      ("youtube.chapters", job.get("youtube", {}).get("chapters")),
                      ("youtube.cover_prompts", job.get("youtube", {}).get("cover_prompts")),
                      ("script_blocks", job.get("script_blocks")),
                      ("image_prompts", job.get("image_prompts")),
                      ("footage_queries", job.get("footage_queries")),
                      ("archive_queries", job.get("archive_queries"))):
        if val is not None and not isinstance(val, list):
            raise SystemExit(
                f"{path} записано как {type(val).__name__}, а должно быть "
                f"списком. Строка здесь разберётся по буквам, а не по "
                f"запятым: \"{path.split('.')[-1]}\": [...]")
    tags = ", ".join(youtube.as_list(job.get("youtube", {}).get("tags"), "tags"))
    if len(tags) > youtube.TAGS_LIMIT:
        raise SystemExit(f"теги занимают {len(tags)} символов при лимите "
                         f"{youtube.TAGS_LIMIT} — youtube.py упадёт на них "
                         f"ПОСЛЕ всего рендера")
    print(f"   списки на месте, теги {len(tags)}/{youtube.TAGS_LIMIT} символов")

    # ШАПКА ШОРТСОВ. Вопрос сверху — то, ради чего шортс досматривают, и
    # спецификация без open_loop даёт два шортса без единой надписи сверху
    # и без вступления (это одно событие ASS). Так уехал ff-ep08: прогон
    # написал предупреждение в лог и завершился успехом, а увидели это уже
    # на готовых файлах. shorts.py теперь подставляет заголовок ролика,
    # но заголовок — страховка, а не замена: он утверждение, а не вопрос.
    print("── шапка шортсов")
    loop = job.get("open_loop") or {}
    per_block = loop.get("questions") or {}
    if not isinstance(per_block, dict):
        raise SystemExit("open_loop.questions должно быть картой "
                         "{\"номер_блока\": \"вопрос\"}, ключ строкой")
    if not (loop.get("question") or "").strip() and not per_block:
        print("   ! нет open_loop — шапка возьмётся из заголовка ролика. "
              "Свой вопрос под каждый блок задаётся в open_loop.questions")
    else:
        bad = [k for k in per_block if not str(k).lstrip("-").isdigit()]
        if bad:
            raise SystemExit(f"open_loop.questions: ключи {bad} — не номера "
                             f"блоков. Ключ строкой: \"0\", \"1\", ...")
        print(f"   вопрос есть, своих по блокам: {len(per_block)}")

    print("── шрифт канала и обложка")
    import type as type_mod
    import covers, shorts
    if not type_mod.font_ok():
        raise SystemExit(f"нет {type_mod.FONT_FILE} — Oswald Bold должен "
                         f"лежать в репозитории")
    print(f"   {type_mod.FONT_FILE.name}, libass «{type_mod.font_name()}»")
    kicker, sub = type_mod.cover_lines(job)
    if not kicker:
        raise SystemExit("пустой cover_kicker — не из чего собрать превью")
    prompts = (job.get("youtube") or {}).get("cover_prompts") or []
    if isinstance(prompts, list) and prompts and len(prompts) != 2:
        raise SystemExit(f"youtube.cover_prompts: {len(prompts)} шт., нужно ровно 2")
    ctr_problems = covers.check_job_covers(job)
    if ctr_problems:
        raise SystemExit("обложки CTR: " + "; ".join(ctr_problems))
    composed = covers.art_prompts(job)
    if job["id"] == "da-vinci-lost-millions":
        if "LOUVRE" not in kicker:
            raise SystemExit(f"da Vinci kicker не про Лувр: {kicker!r}")
        if "$" not in sub and "450" not in sub:
            raise SystemExit(f"da Vinci sub без суммы: {sub!r}")
        if len(prompts) != 2:
            raise SystemExit("da Vinci: нужны два cover_prompts")
        if prompts[0] == prompts[1]:
            raise SystemExit("da Vinci: два одинаковых сюжета обложки")
        blob0, blob1 = composed[0].lower(), composed[1].lower()
        if "empty" not in blob0 or "frame" not in blob0:
            raise SystemExit("da Vinci A: нужна огромная пустая рама")
        if "salvator" not in blob1 and "walnut" not in blob1:
            raise SystemExit("da Vinci B: нужен Salvator Mundi крупно")
        if "open antique wooden trunk" in blob0 or "red silk" in blob0:
            raise SystemExit("da Vinci A: ствол на полу снова делит кадр")
    if job["id"] == "ff-ep07-kid-icarus-attic":
        if "ATTIC" not in kicker:
            raise SystemExit(f"ep07 kicker не про чердак: {kicker!r}")
        if "9,000" not in sub and "9000" not in sub.replace(",", ""):
            raise SystemExit(f"ep07 sub без суммы: {sub!r}")
        if ("kid icarus" not in composed[0].lower()
                and "nes" not in composed[0].lower()):
            raise SystemExit("ep07 A: нужна коробка Kid Icarus")
    overlay_job = {
        "youtube": {},
        "_превью_промпт": {
            "overlay_text": "FOUND IN AN ATTIC. SOLD FOR $9,000."},
    }
    ok, osb = type_mod.cover_lines(overlay_job)
    if ok != "FOUND IN AN ATTIC" or "9,000" not in osb:
        raise SystemExit(f"overlay не разложился на kicker/sub: {ok!r} / {osb!r}")
    fake = {
        "id": "ctr-fallback",
        "topic": {"slug": "yard-sale",
                  "keywords": ["Ming dynasty porcelain bowl",
                               "Faberge Imperial Egg",
                               "auction records"]},
        "youtube": {"title": "Yard Sale Bowl: $35 to $722,000",
                    "chapters": ["The bowl", "The egg"]},
    }
    fp = covers.art_prompts(fake)
    fail = covers.check_prompts(fp)
    if fail:
        raise SystemExit("fallback CTR: " + "; ".join(fail))
    low = (fp[0] + fp[1]).lower()
    if "ming" not in low and "porcelain" not in low:
        raise SystemExit("fallback не взял keyword-предмет")
    if "antique shop" in low:
        raise SystemExit("fallback снова просит antique shop")
    hooks = covers.visual_hooks(fake)
    if "auction records" in (hooks[0] + hooks[1]).lower():
        raise SystemExit("в герои обложки попал skip_keyword")
    try:
        if not covers.kicker_fits(job):
            raise SystemExit(f"kicker не влезает в TEXT_ZONE: {kicker!r}")
        print(f"   kicker «{kicker}»" + (f" / «{sub}»" if sub else "")
              + " влезает")
    except ImportError:
        print("   ! нет PIL — замер kicker пропущен")
    print("   CTR-блок в обоих промптах, два разных сюжета")

    print("── окна шортсов")
    class _B:
        def __init__(self, start, end, block=0):
            self.start, self.end, self.block = start, end, block

    def _m(text, a, b):
        return {"text": text, "start": a, "end": b}

    frag_marks = [
        _m("On the morning of August the museum closed for maintenance.", 0, 4),
        _m("Here is the part most retellings leave out about the sale.", 4, 8),
        _m("increments, breaking through two hundred million dollars.", 8, 12),
        _m("The room erupted when the gavel fell at four hundred million.", 12, 16),
        _m("The mystery buyer was a prince acting as a proxy.", 16, 20),
        _m("The broken board bought for eleven hundred became the record.", 20, 24),
        _m("That four hundred and fifty million dollars changed the market.", 24, 28),
        _m("Nobody has seen the painting in public since that night.", 28, 32),
    ]
    if not shorts.is_fragment_start(frag_marks[2]["text"]):
        raise SystemExit("increments… не распознан как обрывок")
    w = shorts.window_for(_B(12, 16), frag_marks, 32, min_s=8, max_s=52, block_t0=0)
    if not w:
        raise SystemExit("window_for не собрал тестовый кусок")
    first = frag_marks[w[2]]["text"]
    if shorts.is_fragment_start(first) or first.lower().startswith("increments"):
        raise SystemExit(f"шортс стартует с обрывка: {first!r}")
    print(f"   старт «{first[:48]}…» — не обрывок")

    print("── шапка ASS без жёлтого")
    import tempfile
    td = Path(tempfile.mkdtemp())
    layout = shorts.header_layout("How did a glazier walk out of the Louvre?")
    ass = shorts.build_ass([], layout, td / "q.ass")
    body = ass.read_text(encoding="utf-8")
    if "00D4FF" in body or "FFD400" in body:
        raise SystemExit("в шапке шортса остался жёлтый")
    if "\\p1" in body:
        raise SystemExit("в шапке шортса осталась стеклянная панель")
    if type_mod.font_name() not in body:
        raise SystemExit("в ASS нет шрифта канала")
    if "&H00FFFFFF" not in body:
        raise SystemExit("шапка не белая")
    print("   Oswald, белый, без панели")
    opening = type_mod.write_opening_ass(job, td / "opening.ass")
    otext = opening.read_text(encoding="utf-8")
    if kicker.split()[0] not in otext.upper() and kicker not in otext:
        # ASS экранирует редко; kicker без спецсимволов должен быть как есть
        if "STOLEN" not in otext and kicker[:6] not in otext:
            raise SystemExit("в opening.ass нет kicker")
    if "blur" not in otext.lower() and "\\blur" not in otext:
        raise SystemExit("в названии выпуска нет слоя стекла (blur)")
    print("   opening.ass со стеклом")

    print("── план кадров")
    marks = json.loads((work / "marks.json").read_text())
    total = json.loads((work / "state.json").read_text())["total_audio"]
    av = channel.avoid()
    st = style_mod.StyleEngine(
        job["id"], recent_luts=av["lut"], recent_openings=av["opening"],
        recent_transitions=av["main_transition"], recent_sparks=av["sparks"],
        recent_music=av["music"])
    for k in ("lut", "archive_lut"):
        if job.get(k):
            setattr(st, k, job[k])
    build.apply_style_override(st, job)
    build.check_luts(st)
    shots = build.plan_shots(marks, st, assets := work, total,
                             job.get("reject"), job)
    bounds = build.chapter_boundaries(job, getattr(st, "beats", []), total)
    if bounds:
        shots, total = build.insert_chapter_cards(shots, bounds, total)
        build.attach_card_backgrounds(shots)
        cards = [s for s in shots if s.get("kind") == "card"]
        if any("speed" in s or "move" in s for s in cards):
            raise SystemExit("у карточки главы есть speed/move — rails.metrics "
                             "упадёт на None")
        missing_bg = [s["card_text"] for s in cards if not s.get("card_bg")]
        if missing_bg:
            raise SystemExit(f"нет фона следующей истории: {missing_bg}")
        print(f"   {len(cards)} карточек глав, стекло на кадре следующей истории")
    build.set_render_durations(shots)
    sec, cnt, mt = build.material_report(shots)
    end = shots[-1]["start"] + shots[-1]["duration"]
    print(f"   {len(shots)} кадров, {total/60:.1f} мин, "
          f"генерация {sec['gen']/mt*100:.1f}%")
    if abs(end - total) > 0.5:
        raise SystemExit(f"таймлайн разошёлся со звуком на {abs(end-total):.2f} с")
    print(f"   таймлайн сходится со звуком ({abs(end-total):.3f} с)")
    print("\nСМОУК-ПРОГОН ПРОЙДЕН")


if __name__ == "__main__":
    main(sys.argv[1])
