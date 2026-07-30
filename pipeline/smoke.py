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


def main(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
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
    print(f"   {n_ch} глав, начала уникальны")

    print("── план кадров")
    marks = json.loads((work / "marks.json").read_text())
    total = json.loads((work / "state.json").read_text())["total_audio"]
    av = channel.avoid()
    st = style_mod.StyleEngine(
        job["id"], recent_luts=av["lut"], recent_openings=av["opening"],
        recent_transitions=av["main_transition"], recent_sparks=av["sparks"])
    for k in ("lut", "archive_lut"):
        if job.get(k):
            setattr(st, k, job[k])
    build.apply_style_override(st, job)
    build.check_luts(st)
    shots = build.plan_shots(marks, st, assets := work, total,
                             job.get("reject"), job)
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
