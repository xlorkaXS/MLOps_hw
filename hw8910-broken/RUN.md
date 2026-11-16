# Воспроизведение ДЗ8–10

Из hw8910-broken: `uv sync --locked --python 3.12`, затем `make check`. Для реальной модели добавить `--extra model` к sync. Без зависимостей доступен `make check-contract`. Сохранённые доказательства — в submission/, их чтение не требует запуска сервисов.

## Локальный Prefect

Запустить в отдельном терминале на время опыта:

    PREFECT_HOME="$PWD/.prefect" PREFECT_SERVER_ANALYTICS_ENABLED=false .venv/bin/prefect server start --host 127.0.0.1 --port 8420

В другом терминале:

    export PREFECT_HOME="$PWD/.prefect"
    export PREFECT_API_URL=http://127.0.0.1:8420/api
    .venv/bin/python experiments.py .lab-prefect-new .lab-proof-output

Не использовать уже существующий workspace. Для одиночного стенда: `.venv/bin/python lab.py --workspace .lab-api init`, затем `.venv/bin/python service.py --workspace .lab-api --warmup-seconds 10`. При POST /generate передать JSON с prompt и, опционально, max_new_tokens=32. Изменение accepted требует перезапуска. Ctrl+C штатно завершает свои процессы.

## Реальный запуск на MPS

1. Восстановить данные оценки ДЗ6–7; sibling hw67-broken остаётся источником кода гейта и протокола. Скачайте Qwen/Qwen3-0.6B с ревизией из manifest в кеш, если её нет. Создайте новый workspace, например .lab-real-new, и поместите оригинальный адаптер ДЗ6–7 в .lab-real-new/models/adapter. Сравните с хешами из submission/release/manifest-candidate.json. [Отчёт](REPORT.md) объясняет, как получить пакет из реестра.
2. `HF_HUB_OFFLINE=1 .venv/bin/python evaluate_release.py --workspace .lab-real-new` — повторная оценка через общий runtime. На CPU потребуется отдельно закрепить другой runtime и повторить оценку; существующий MPS-отчёт его не покрывает.
3. `HF_HUB_OFFLINE=1 .venv/bin/python bootstrap.py --workspace .lab-real-new` — однократное принятие исходной базы как точки отсчёта.
4. `HF_HUB_OFFLINE=1 .venv/bin/python flow.py --workspace .lab-real-new --mode real` — ожидается ненулевой код от проверки качества кандидата, указатель неизменен. Не продолжать выпуск при отказе.
5. `HF_HUB_OFFLINE=1 .venv/bin/python smoke.py .lab-real-new .lab-http-output` — два старта FastAPI, HTTP smoke, 8+8 запросов, штатная остановка. Для ручной работы: `.venv/bin/python service.py --backend real --workspace .lab-real-new --warmup-seconds 0`; после проверки — Ctrl+C.

Не коммитить рабочие каталоги .lab*, кеши, окружение, веса или volumes. Не запускайте MLflow/Colima, если адаптер уже восстановлен. Официальная документация жизненного цикла FastAPI: https://fastapi.tiangolo.com/advanced/events/ .
