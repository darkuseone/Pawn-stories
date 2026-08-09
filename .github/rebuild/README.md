# Принудительный перезапуск сборки

Имя файла = id джоба без `.json` (должен существовать `jobs/<имя>.json`).

Первая строка файла — `stage`:

- `auto` — полный прогон (по умолчанию, если строка пустая/битая)
- `render` — монтаж + упаковка + шортсы из кэша `assets` (без озвучки и генерации)
- `post` / `shorts` / `material` / `vet` / `assets` — как у `workflow_dispatch`

Пример пересборки монтажа без озвучки:

```
echo render > .github/rebuild/garage-sale-millions-ming-durer-faberge
git add .github/rebuild/garage-sale-millions-ming-durer-faberge
git commit -m "rebuild render: stereo fix"
git push
```
