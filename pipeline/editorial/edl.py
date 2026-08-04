"""
edl.py — МОНТАЖНЫЙ ЛИСТ и доказательство процесса.

Зачем файл, который не влияет на картинку
-----------------------------------------
Когда площадка спрашивает, чем этот ролик отличается от поточной сборки,
показывать нечего: готовый mp4 выглядит как готовый mp4. Нужен документ,
из которого видно, ЧТО и ПОЧЕМУ решено на каждой склейке. Здесь он и
собирается — из решений, которые движок уже принял, поэтому стоит он ноль
и не может разойтись с роликом: строится из того же плана, что ушёл в
рендер.

Три выгрузки
------------
  edl.txt        монтажный лист в формате CMX3600. Его открывает любая
                 монтажная программа: план можно загрузить в Resolve или
                 Premiere и увидеть тот же таймлайн покадрово
  timeline.md    человекочитаемый разбор: доли, замысел каждой, почему
                 кадр такой длины, где стоят выдохи и плашки
  decisions.json машинное представление целиком — вектор стиля, доли,
                 метрики плана, замечания проверки

Формат тайм-кода везде HH:MM:SS:FF при частоте кадров ролика.
"""

import json
from datetime import datetime
from pathlib import Path


def tc(seconds: float, fps: int) -> str:
    """Секунды -> HH:MM:SS:FF."""
    seconds = max(0.0, float(seconds))
    total_frames = int(round(seconds * fps))
    f = total_frames % fps
    s = (total_frames // fps) % 60
    m = (total_frames // (fps * 60)) % 60
    h = total_frames // (fps * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


# ─────────────────────────── CMX3600 ───────────────────────────

def write_edl(shots, out: Path, fps: int, title: str):
    """
    Монтажный лист CMX3600.

    Событие на кадр. Переход пишется как DISSOLVE с длиной в кадрах — это
    единственное, что формат умеет из наших переходов, поэтому имя
    конкретного xfade уходит в комментарий рядом. Так лист остаётся
    загружаемым в монтажку и при этом не теряет наших решений.
    """
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec = 0.0
    for n, sh in enumerate(shots, 1):
        src = Path(str(sh.get("source_file") or sh.get("file"))).name
        reel = (src.split("_")[0][:8] or "AX").upper()
        src_in = float(sh.get("src_start", 0.0))
        dur = float(sh["duration"])
        tr = sh.get("transition", "cut")
        trd = float(sh.get("transition_dur", 0.0) or 0.0)

        if tr == "cut" or trd <= 0:
            kind = "C"
            frames = ""
        else:
            kind = "D"
            frames = f"{max(1, int(round(trd * fps))):03d}"

        lines.append(
            f"{n:03d}  {reel:<8} V     {kind}        {frames:<4}"
            f"{tc(src_in, fps)} {tc(src_in + dur, fps)} "
            f"{tc(rec, fps)} {tc(rec + dur, fps)}")
        lines.append(f"* FROM CLIP NAME: {src}")
        lines.append(f"* MATERIAL: {sh.get('tag', '?')}  "
                     f"MOVE: {sh.get('move') or '-'}  "
                     f"XFADE: {tr}")
        if sh.get("why"):
            lines.append(f"* DECISION: {sh['why']}")
        if sh.get("beat_kind"):
            lines.append(f"* BEAT: {sh['beat_kind']}")
        lines.append("")
        rec += dur

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ─────────────────────────── РАЗБОР ДЛЯ ЧЕЛОВЕКА ───────────────────────────

def write_timeline(shots, beats, style_card, pacing_report, findings,
                   metrics, out: Path, fps: int, title: str,
                   divergence=None, text_moments=None):
    """
    Монтажный лист словами. Это тот документ, который отправляют человеку.

    Пишется так, чтобы его можно было читать без знания конвейера: сначала
    решения на уровне ролика, потом доли с объяснением каждой, потом
    таблица кадров, потом проверки.
    """
    L = []
    A = L.append

    A(f"# Монтажный лист — {title}")
    A("")
    A(f"Собран {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
      f"Частота {fps} к/с. Кадров {len(shots)}.")
    A("")
    A("Документ строится из того же плана, что ушёл в рендер: расхождение "
      "между ним и роликом невозможно технически.")
    A("")

    # ── решения уровня ролика ──
    A("## Решения по ролику")
    A("")
    A("| Что | Значение | Почему так |")
    A("|---|---|---|")
    A(f"| Арка темпа | `{style_card.get('arc')}` | "
      f"форма кривой плотности склеек на весь ролик |")
    A(f"| Открытие | `{style_card.get('opening')}` | "
      f"первые секунды решают удержание, вариант разводится с прошлыми |")
    A(f"| Основной переход | `{style_card.get('main_transition')}` | "
      f"звучит в {float(style_card.get('transition_focus', 0))*100:.0f}% "
      f"склеек, остальное — из общего набора |")
    A(f"| Базовый кадр | {style_card.get('base_duration')} с | "
      f"опорная длительность, дальше её мнут доли и арка |")
    A(f"| Цветокор | `{style_card.get('lut')}` / "
      f"`{style_card.get('archive_lut')}` | "
      f"второй — только для подлинных архивных фото |")
    A(f"| Доля генерации | "
      f"{float(style_card.get('generated_share', 0))*100:.0f}% | "
      f"остальное — реальный сток и архив |")
    A(f"| Плашки | `{style_card.get('text_style')}` | "
      f"кинетическая типографика на числах |")
    A(f"| Подложка | ducking `{style_card.get('duck_style')}`, "
      f"глубина {style_card.get('duck_depth')} | "
      f"музыка уходит под голос в отмеченных точках |")
    A("")

    if divergence:
        A("### Разведение с предыдущими роликами")
        A("")
        A(f"Учтено роликов в памяти: **{divergence.get('history', 0)}**. "
          f"Попыток пересдачи: **{divergence.get('tries', 1)}**.")
        A("")
        if divergence.get("differs_from_prev"):
            A(f"Расстояние до предыдущей загрузки: "
              f"**{divergence.get('distance_to_prev')}** "
              f"(порог 0.28). Значимо разошлись оси:")
            A("")
            for ax in divergence["differs_from_prev"]:
                A(f"- `{ax}`")
            A("")
        A(f"_{divergence.get('note', '')}_")
        A("")

    # ── доли ──
    A("## Нарративные доли")
    A("")
    A("Ритм считается от структуры сценария, а не от таймера. Каждая доля "
      "получает свой темп, свою долю стока и свою амплитуду камеры.")
    A("")
    A("| # | Начало | Длина | Тип | Почему так классифицирована |")
    A("|---|---|---|---|---|")
    for i, b in enumerate(beats, 1):
        A(f"| {i} | {tc(b.start, fps)} | {b.duration:.0f} с | "
          f"**{b.kind}** | {b.reason} |")
    A("")

    if pacing_report:
        A("### Кривые темпа")
        A("")
        for row in pacing_report:
            A(f"- {row.strip()}")
        A("")

    # ── плашки ──
    if text_moments:
        A("## Плашки (кинетическая типографика)")
        A("")
        A("Собственная графика конвейера: этих элементов нет ни в одном "
          "исходном материале.")
        A("")
        A("| Тайм-код | Текст | Стиль | Раскладка |")
        A("|---|---|---|---|")
        for m in text_moments:
            A(f"| {tc(m['t'], fps)} | {m['text']} | `{m['style']}` | "
              f"`{m['place']}` |")
        A("")

    # ── кадры ──
    A("## Кадры")
    A("")
    A("| # | Тайм-код | Длина | Материал | Движение | Переход | Решение |")
    A("|---|---|---|---|---|---|---|")
    rec = 0.0
    for n, sh in enumerate(shots, 1):
        src = Path(str(sh.get("source_file") or sh.get("file"))).name
        A(f"| {n} | {tc(rec, fps)} | {sh['duration']:.1f} с | "
          f"{sh.get('tag', '?')} `{src}` | "
          f"{sh.get('move') or '—'} | {sh.get('transition', '—')} | "
          f"{sh.get('why', '')} |")
        rec += sh["duration"]
    A("")

    # ── проверки ──
    A("## Проверка плана на признаки шаблона")
    A("")
    A("| Метрика | Значение | Что означает |")
    A("|---|---|---|")
    A(f"| Разброс длительностей | {metrics.get('cv_duration')} | "
      f"ниже 0.22 — нарезка по таймеру |")
    A(f"| Разброс скоростей камеры | {metrics.get('speed_stdev')} | "
      f"ниже 0.06 — все наезды одинаковы |")
    A(f"| Доля главного перехода | "
      f"{float(metrics.get('top_transition_share', 0))*100:.0f}% | "
      f"выше 78% — единственный почерк склейки |")
    A(f"| Разных переходов | {metrics.get('distinct_transitions')} | |")
    A(f"| Разных движений камеры | {metrics.get('distinct_moves')} | |")
    A(f"| Доля стокового видео | "
      f"{float(metrics.get('clip_share', 0))*100:.0f}% | |")
    A("")
    if findings:
        A("Замечания:")
        A("")
        for f in findings:
            A(f"- **{f.level}** `{f.code}` — {f.text}")
    else:
        A("Замечаний нет.")
    A("")

    out.write_text("\n".join(L), encoding="utf-8")
    return out


# ─────────────────────────── МАШИННОЕ ───────────────────────────

def write_decisions(out: Path, *, style_card, vector, beats, metrics,
                    findings, divergence, text_moments, pacing_decisions,
                    shots):
    """
    Всё принятое одним JSON. Отсюда channel.py берёт, что записать в журнал,
    и отсюда же поднимается статистика при разборе дрейфа канала.
    """
    payload = dict(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        style=style_card,
        style_vector={k: (list(v) if isinstance(v, tuple) else v)
                      for k, v in vector.items()},
        divergence=divergence or {},
        beats=[b.summary() for b in beats],
        pacing=pacing_decisions or {},
        text_moments=text_moments or [],
        metrics=metrics,
        findings=[dict(level=f.level, code=f.code, text=f.text)
                  for f in findings],
        shots=[dict(n=i + 1,
                    start=round(s["start"], 3),
                    duration=round(s["duration"], 3),
                    kind=s["kind"], tag=s.get("tag"),
                    file=Path(str(s.get("source_file") or s.get("file"))).name,
                    move=s.get("move"), speed=s.get("speed"),
                    transition=s.get("transition"),
                    transition_dur=s.get("transition_dur"),
                    effect=s.get("effect"),
                    beat=s.get("beat_kind"),
                    why=s.get("why"))
               for i, s in enumerate(shots)],
    )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return out


def export_all(out_dir: Path, *, shots, beats, style_card, vector, metrics,
               findings, divergence, text_moments, pacing_report,
               pacing_decisions, fps: int, title: str):
    """Все три выгрузки разом. Возвращает пути."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return dict(
        edl=write_edl(shots, out_dir / "edl.txt", fps, title),
        timeline=write_timeline(
            shots, beats, style_card, pacing_report, findings, metrics,
            out_dir / "timeline.md", fps, title,
            divergence=divergence, text_moments=text_moments),
        decisions=write_decisions(
            out_dir / "decisions.json", style_card=style_card, vector=vector,
            beats=beats, metrics=metrics, findings=findings,
            divergence=divergence, text_moments=text_moments,
            pacing_decisions=pacing_decisions, shots=shots),
    )
