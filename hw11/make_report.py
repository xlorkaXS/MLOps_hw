"""Render report.html from actual artifacts, with a printable neutral palette."""
import html
import json
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parent
R=ROOT/"results"
s=json.loads((R/"summary.json").read_text())
m=s["stats"]
h=s["hpa"]
w=s["prom_comparison"]
c=json.loads((R/"checks.json").read_text())
image=json.loads((R/"api-image.json").read_text())
E=html.escape

def p(x): return "<p>"+E(x)+"</p>"
def heading(x): return "<h2>"+E(x)+"</h2>"
def table(columns, rows):
    return "<table><thead><tr>"+"".join("<th>"+E(str(c))+"</th>" for c in columns)+"</tr></thead><tbody>"+"".join(
        "<tr>"+"".join("<td>"+E(str(v))+"</td>" for v in r)+"</tr>" for r in rows)+"</tbody></table>"
def plot(name,caption):
    return f'<figure><img src="results/{name}.png" alt="{E(caption)}"><figcaption>{E(caption)}</figcaption></figure>'
def fmt(value):
    return f"{value:.3f}" if value is not None else "—"
def server_metric(name):
    vals=w["server"][name]["result"]["data"]["result"]
    return float(vals[0]["value"][1]) if vals else None

parts=['<p class="label">MLOPS-26 · ДЗ 11 · 17 ноября 2026 (МСК)</p>',
       '<h1>Сервис работает, пользователи ждут</h1>',
       p('Kind, Helm и FastAPI. Все результаты ниже получены по HTTP из четырёх Pod. Backend: simulated. TTFC — время до первой части ответа, inter-chunk — между частями. Модель не генерирует токены; TTFT, TPOT и tokens/s не измерялись.'),
       heading('Стенд и происхождение'),
       p(f'Apple Silicon, локальная Colima hw11, 3 CPU, 5 ГиБ RAM, виртуальный диск 20 ГиБ; до запуска на хосте свободно около 244 ГиБ. Kind 0.33.0, Kubernetes 1.36.4, Helm 4.3.2. Образ API {image["architecture"]}, {image["size"]/1048576:.1f} МиБ (docker inspect). Точные imageID, Docker version, размеры kind/VM и состав контейнеров — в results. Лимиты Pod API 500m/160Mi, requests 100m/64Mi. По умолчанию две реплики.'),
       p('Prometheus обнаруживает каждый API Pod, собирает CPU/RAM через kubelet cAdvisor и Ready через kube-state-metrics. Хранилище Prometheus ограничено retention 6 ч / 180 МБ и emptyDir 250 МиБ, Grafana — emptyDir 100 МиБ. Экспорт dashboard в monitoring/dashboard.json и results/grafana-export.json. Request/session ID только в логах, не в labels.'),
       p('Immutable ConfigMap содержит маленький model.json. Имя ConfigMap и ожидаемый SHA-256 вычисляются из этих байтов; загрузчик сверяет хеш и возвращает загруженный model_id и runtime_sha256. Тест замены проверил ту же версию по фактическому ответу. Два диалога передают контекст с запросом; локальный кеш не является хранилищем истории. Для настоящих весов потребуются неизменяемый URI и общедоступное хранилище/локальные копии с хешем; один RWO-том не решает доступ с разных узлов.'),
       heading('A. Прогрев, rollout и откат'),
       p('Гипотеза: если readiness читает только жизнь процесса, Service пускает запросы до прогрева. Подняли 2 Pod с 20-секундным прогревом, затем повторили 28 секунд по 2 запроса/с после перевода startup на /startup, readiness на /ready. Liveness проверяет /live. Startup budget 65 с. Исправление проверено и прямым обращением к непрогретому Pod: там 503 и он исключён из EndpointSlice.'),
       table(['Опыт','Запланировано','Завершено','Статусы','Иные исходы'],
             [[x,m[x]["planned"],m[x]["completed"],str(m[x]["statuses"]),str(m[x]["outcomes"])]
              for x in ('A-defect','A-fixed-final','A-fixed-gated','A-rollout','A-rollout-final','A-bad-B') if x in m]),
       p('Диагностика: Helm сохранял overrides предыдущего выпуска, если upgrade запускался без новых значений. Это видно по пилотному A-rollout-confirmed, где длинный поток был B, а не A. Для зачётных повторов добавлен --reset-values, версия проверяется на ответах; pilot и A-fixed без явного reset не используются как доказательство.'),
       plot('01-rollout','Ready и Service endpoints по времени. Маркеры — реальные HTTP 503.'),
       p('RollingUpdate: maxSurge=1, maxUnavailable=0, drain в preStop и Uvicorn graceful 35 с внутри grace 45 с. Длинный NDJSON-поток начат на A до смены; принятие нового B не подменяет ID старого потока. При повторе готовности A-fixed-final был один 503 от старого уже завершаемого Pod, не от нового. Финальный preStop сначала ждёт 3 с на распространение удаления endpoint, затем закрывает приём (drain) и ждёт 1 с. Повтор A-fixed-gated выполнен с этим порядком; это эмпирический запас в локальном кластере, а не атомарная гарантия маршрутизации.'),
       p('Первый rollout на 2,4 req/s выявил конечность очереди: длительный поток занимает слот и возможен честный 429. Этот опыт сохранён, а повтор с меньшим допущенным потоком проверяет штатный сценарий. Это не обещание нулевых ошибок при произвольной перегрузке.'),
       p('Неготовая B: neverReady, старые Ready Pod продолжают отвечать A. После helm rollback фактический ответ снова A. Отдельно остановили контейнер без grace после первого фрагмента: незавершённый поток отмечен aborted, контейнер восстановился с тем же model_id. Клиентских ретраев не было.'),
       heading('B. ClientIP и горячая реплика'),
       p('Два ClusterIP Service имеют один селектор. Kind iptables/kindnet; четыре источника задокументированы в client-ips.json. Connection: close, новый TCP на запрос; нет смешивания с HTTP keep-alive. Таблица по Pod хранит client→Pod. Для ClientIP важен IP источника на этом участке сети, а не cookie или session_id; при прокси/NAT гранулярность другой.'),
       p('Ровный вход и 80% от клиента 0. Для сравнения политик в каждом профиле одни и те же тела, слоты и параметры; 4,4 req/s, 528 запросов/120 с, 4 фрагмента, интервал 100 мс, deadline чтения сокета 35 с. Размер локального LRU 12, hit 40 мс, miss 280 мс, один running + восемь waiting. Каждый замер начинается с явно очищенных кешей и прогретых процессов, сброс не меняет отсчёт привязки Service.'),
       table(['Фаза','План / отправлено / успех','TTFC p50 / p95, с','Полный p50 / p95, с','Inter-chunk p95, с','Неусп. / timeout','Макс. сдвиг отправки, с'],
             [[k,f'{v["planned"]} / {v["sent"]} / {v["completed"]}',
              f'{fmt(v["ttfc_s"]["p50"])} / {fmt(v["ttfc_s"]["p95"])}',
              f'{fmt(v["total_s"]["p50"])} / {fmt(v["total_s"]["p95"])}',fmt(v["inter_chunk_s"]["p95"]),
              f'{v["failure_rate"]:.1%} / {v["timeout"]}',fmt(v["max_send_delay_s"])]
              for k,v in sorted(m.items()) if k.startswith("B-")]),
       plot('02-by-pod','Сравнение p95 очереди и TTFC. n — число успешных ответов реплики.'),
       plot('03-policies','По три прогона на комбинацию; не смешиваем выборки и не скрываем отказы.')]
for k in ('B-1-skew-sticky','B-1-skew-plain','B-1-uniform-sticky'):
    pods=m[k]["pods"]
    names=set(m['B-1-skew-sticky']['pods']) | set(m['B-1-skew-plain']['pods'])
    parts += [p(k), table(['Pod','Получено / успех / отказ','По клиентам','Cache hit','Очередь p95','TTFC p95'],
        [[pod,f'{v["observed"]}/{v["completed"]}/{v["rejected"]}',str(v["clients"]),f'{(v["hit_rate"] or 0):.1%}',
          fmt(v["queue_s"]["p95"]),fmt(v["ttfc_s"]["p95"])]
         for pod,v in pods.items()]+[[pod, '0 / 0 / 0', '—', '—', '—', '—'] for pod in sorted(names-set(pods))])]
def med(profile,policy,key):
    rows=[m[f'B-{i}-{profile}-{policy}'] for i in (1,2,3)]
    return statistics.median(r['ttfc_s']['p95'] if key=='p95' else r['failure_rate'] for r in rows)

parts += [p(f'Правка для перекоса 80/20: ClientIP → None. Медиана p95 TTFC по трём прогонам: {med("skew","sticky","p95"):.3f} → {med("skew","plain","p95"):.3f} с. Медианная доля неуспеха: {med("skew","sticky","fail"):.1%} → {med("skew","plain","fail"):.1%}. Это сравнение повторов, не оценка перцентиля из объединённых наблюдений.'),
          p('Сопоставляем ошибки, очереди, распределение и hit rate; даже если IP-привязка лучше сохраняет LRU, рост очереди может перевешивать выгоду. Ровный вход слабо нагружает один источник, но не гарантирует равное распределение: несколько IP могут попасть в один Pod. Случай на None под перекошенным входом показывает, что большой клиентский IP сам по себе не обязан перегружать один Pod при новом TCP на запрос. Отдельного вывода о production inference engine эти числа не дают.'),
          heading('C. HPA, алерты и перцентили'),
          p(f'CPU-нагрузка: 10 req/s, 80% с одного IP, 0,22 секунды CPU работы на принятый запрос; target 75% от requests CPU; resource metrics API подтверждён. Максимальная наблюдаемая средняя загрузка HPA — {h["max_observed_cpu_percent"]}% от requests. Третий Pod появился через {h["pod3_seconds"]:.1f} с, стал Ready через {h["ready3_seconds"]:.1f} с. Возврат к 2 после окончания входа — {h["back_to_2_after_idle_seconds"]:.1f} с, включая окно стабилизации 45 с и дискретизацию метрик.'),
          plot('04-hpa','Создание и готовность Pod показаны отдельно, интенсивность по правой оси.'),
          table(['Алерт','Первый pending, с','Первый firing, с'],
                [[k,fmt(v.get("pending")),fmt(v.get("firing"))] for k,v in h["alert_first_seconds"].items()]),
          p('Оба алерта удерживают условие 9 с; окно 30 с. SlowFirstChunk: p95 TTFC >0,9 с и ≥12 завершений. TooManyRefusals: доля отказов >10% и ≥20 наблюдений. После снижения нагрузки список активных алертов пуст. Это проверка состояний Prometheus, а не доставка уведомлений через Alertmanager.'),
          p('Средняя CPU для HPA не различает очередь горячей реплики и простой холодной. Sticky-соединения/выбор по IP не переносятся на новую реплику при её появлении; основная доля нагрузки может остаться на старой. Таблица распределения запроса ниже фиксирует именно наблюдаемые назначения.'),
          table(['CPU-фаза, Pod','Всего','Завершено','Отклонено','Клиенты'],
                [[k,v["observed"],v["completed"],v["rejected"],str(v["clients"])] for k,v in m["C-heavy-sticky"]["pods"].items()]
                + [[k,0,0,0,'—'] for k in h["new_pods"] if k not in m["C-heavy-sticky"]["pods"]]),
          p(f'Сравнение одного 96-секундного окна внутри {w["phase"]}: клиент n={w["n_client"]}, p95 TTFC {fmt(w["client"]["ttfc_s"])} с и полный {fmt(w["client"]["total_s"])} с; Prometheus n≈{fmt(server_metric("count"))}, p95 {fmt(server_metric("ttfc_p95"))} с и {fmt(server_metric("total_p95"))} с. Клиент включает сетевой путь; сервер пишет Histogram после завершения потока. PromQL суммирует increase по bucket с le до histogram_quantile, а не усредняет p95 реплик. Интерполяция bucket, scrape и граничные завершения дают различие.'),
          '<pre>'+E(w["server"]["ttfc_p95"]["expr"])+'</pre>',
          heading('Пять собственных проверок'),
          table(['Проверка','Результат'],[[k,'PASS' if value else 'FAIL'] for k,value in c.items() if k!="evidence"]),
          p('Сырые результаты содержат план, фактическое время отправки, первый чанк, завершение, статус, модель/Pod и все интервалы. Успех требует маркера done и ожидаемого числа частей; отказ, timeout и оборванный HTTP 200 не входят в перцентили успеха, но входят в знаменатель. При SIGKILL сервер не успевает инкрементировать счётчик aborted, поэтому источник для аварийного потока — клиентская трасса. Защита от опоздания генератора фиксирует expired, не скрывая недоотправленные запросы.'),
          heading('Ревью PR: не привязывать диалог к Pod навсегда'),
          p('Запрос ещё не начался: при известных оценках отправляю на B. У A только ожидание около 3 с; у свободной B пересчёт 0,8 с. Если остальная стоимость сопоставима, B выигрывает около 2,2 с до первого ответа. Это не правило без условий: нужны измерения очереди и prefill по длинам, пропускная способность декодирования, hit/miss, свежесть сигналов и распределение задержек. Сравниваю прогноз queue + prefill до дедлайна, учитываю доступный слот и лимиты, а не только affinity. При насыщении обоих — явный 429/503 с бюджетом повтора, без бесконечной очереди.'),
          p('Привязку делаю предпочтением, а не обязательством. История переносится в запросе или сохраняется вне Pod, префиксный кеш лишь ускоряет вычисление. При потере A восстанавливаю историю на B и пересчитываю кеш. Начатый поток не перекидываю между Pod: при плановом обновлении даю ему завершиться, при аварии помечаю незавершённым и разрешаю новую попытку с тем же диалогом и новым request ID, не склеивая частичные ответы как один успешный.'),
          '<p>Первичный кейс: <a href="https://developers.redhat.com/articles/2026/01/13/accelerate-multi-turn-workloads-llm-d">Red Hat: multi-turn inference с llm-d</a>. Он сравнивает простую балансировку и маршрутизацию с учётом кеша; для нашего случая важно добавить стоимость ожидания. Материала самой лекции 11 в выданной папке нет, поэтому совпадение кейса с конкретным слайдом не проверено. См. также <a href="https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/">Kubernetes HPA</a> и <a href="https://prometheus.io/docs/practices/histograms/">Prometheus histograms</a>.</p>',
          heading('Воспроизводимость и ограничения'),
          p('Один локальный узел, CPU-имитатор, маленькие контексты и фиксированные задержки; здесь нет эксплуатационного SLA для внешней сети, GPU или реальной генерации. Не обещаем точного повторения назначения случайных TCP-соединений. Команды запуска/удаления и лимиты — в README.md; приложены версии, полная трасса, экспорт Grafana и манифесты. Кластер остановлен после сбора данных.')]
css="""*{box-sizing:border-box}body{margin:0;background:#f8f5f3;color:#282329;font:17px/1.58 system-ui,sans-serif}main{max-width:1160px;margin:auto;padding:42px 34px 64px}h1{font-size:43px;line-height:1.15}h2{font-size:27px;margin-top:44px;border-bottom:1px solid #c4aab5;padding-bottom:10px}a{color:#754354}.label{letter-spacing:.08em;color:#6a4d5d}table{border-collapse:collapse;width:100%;font-size:.86em;margin:18px 0}td,th{padding:8px;border:1px solid #cfbfc4;vertical-align:top;overflow-wrap:anywhere}th{background:#efe4e8}figure{margin:25px 0}img{display:block;width:100%;border:1px solid #cfbfc4}figcaption{color:#51454d;font-size:.9em}pre{white-space:pre-wrap;background:#efe4e8;padding:15px} @media print{body{background:white}main{padding:0}figure,tr{break-inside:avoid}}"""
(ROOT/"report.html").write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>ДЗ11 · Kubernetes</title><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style><main>'+'\n'.join(parts)+'</main></html>')
