# ДЗ11 — Kubernetes под нагрузкой

Продолжение FastAPI из ДЗ8–10: сохранены health/ready, фиксированный лимит генерации и идентификация фактически загруженной версии. Для K8s добавлены NDJSON-поток, локальная очередь и строго помеченный CPU-имитатор. Нет weights, токенов модели, облачного API или затрат на GPU.

## Изолированный повтор

Не применять манифесты к рабочему кластеру. Нужны Docker, kind, kubectl и Helm. На Apple Silicon проверено с Colima, отдельным профилем и отдельным kubeconfig. Диск 20 ГиБ — верхняя ёмкость виртуального диска, не фактический размер файлов.

```sh
df -h .
colima start hw11 --cpu 3 --memory 5 --disk 20 --activate=false
export DOCKER_HOST="unix://$HOME/.colima/hw11/docker.sock"
export KUBECONFIG="/absolute/path/to/isolated/hw11-kubeconfig"
docker info --format '{{.ServerVersion}} CPU={{.NCPU}} RAM={{.MemTotal}}'
kind create cluster --name hw11 --config kind.yaml \
  --image kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed --wait 90s
docker build -t hw11-api:1.0.0 .
kind load docker-image hw11-api:1.0.0 --name hw11
kubectl apply -f clients.yaml
kubectl rollout status statefulset/load --timeout=90s
sh monitoring/install.sh
```

Для нового прогона делайте копию проекта без существующего results/ либо предварительно переместите результаты: журналы запросов и timeline пишутся append. Кластер нужен только во время эксперимента.

```sh
helm lint chart
python3 tests.py
python3 -u run.py A    # дефект, исправление, длинный поток, неготовая B и rollback
python3 -u run.py B    # 12 фаз × 120 секунд, без HPA
python3 -u operate.py verify  # принудительный стоп, проверки замены и диалогов
python3 -u operate.py c       # HPA, CPU и алерты
python3 -u collect.py
uv run --with 'matplotlib==3.11.2' python analyze.py
python3 make_report.py
```

Prometheus и Grafana доступны только внутри кластера; для просмотра временно используйте port-forward к svc/prometheus:9090 или svc/grafana:3000. Демонстрационные endpoints drain/clear не предназначены для внешнего доступа. Для реальной эксплуатации их ограничиваем сетью/авторизацией; отключённую проверку kubelet TLS оставляем только на этом локальном стенде.

## После эксперимента

```sh
# Сначала сохранить results/. Завершить свои port-forward, если они запускались.
kind delete cluster --name hw11
colima stop hw11
# Если хотите освободить место и больше не нужен отдельный профиль:
# colima delete hw11
```

Для сохранения готового кластера достаточно штатного `colima stop hw11`. Не трогайте главный профиль Colima, чужие контейнеры и тома. Архив сдачи не включает kubeconfig, секреты, контейнерные образы или диски VM.

## Файлы

- `chart/` — Deployment, два Service, хешированный immutable ConfigMap артефакта, probes и HPA.
- `monitoring/` — discovery, histogram PromQL, два алерта и дашборд.
- `results/` — 120-секундные трассы, Ready/EndpointSlice, журнал отказов, HPA, Grafana export, четыре графика. `results/pilot/` отдельно содержит первоначальную диагностику сборки, не часть статистики.
- `report.html` — разбор измерений и ревью PR. Видео и личная инструкция передаются отдельно.
