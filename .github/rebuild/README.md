# Touch a file named after the job (no .json) to force a rebuild.

# Тело файла — стадия: material, vet, assets, render, post, shorts, auto.
# Пустое или что угодно ещё → auto, как раньше.
#
# material и vet пишут новый футаж и vetted.json в кэш assets-<id>-v1-;
# без этого следующий render поднял бы старый пул. render/post/shorts
# кэш не перезаписывают.
