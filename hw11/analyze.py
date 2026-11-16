"""Make four charts and statistics from unmodified request and control-plane evidence."""
from collections import Counter, defaultdict
from pathlib import Path
import json
import math
import statistics

ROOT = Path(__file__).resolve().parent
OUT = ROOT/"results"


def read(name):
    return json.loads((OUT/name).read_text())


def lines(name):
    return [json.loads(s) for s in (OUT/name).read_text().splitlines() if s.strip()]


def quantile(values,p):
    x=sorted(values)
    if not x: return None
    h=(len(x)-1)*p
    i=int(h)
    return x[i]+(x[min(i+1,len(x)-1)]-x[i])*(h-i)


def group_stats(rows):
    good=[r for r in rows if r["disposition"]=="completed"]
    by_pod=defaultdict(list)
    for r in rows:
        if r.get("pod"): by_pod[r["pod"]].append(r)
    def metrics(rs):
        intervals=[y-x for r in rs for x,y in zip(r.get("chunk_times",[]),r.get("chunk_times",[])[1:])]
        out={}
        for name,samples in [(k,[r[k] for r in rs if r.get(k) is not None])
                for k in ("ttfc_s","queue_s","total_s")]+[("inter_chunk_s",intervals)]:
            out[name]={"n":len(samples),"p50":quantile(samples,.5),"p95":quantile(samples,.95)}
        out["hit_rate"]=sum(r.get("cache_hit",False) for r in rs)/len(rs) if rs else None
        return out
    return dict(planned=len(rows),sent=sum(r.get("sent_monotonic") is not None for r in rows),
                outcomes=dict(Counter(r["disposition"] for r in rows)),statuses=dict(Counter(str(r.get("status")) for r in rows)),
                versions=dict(Counter(r.get("model_id") or "unknown" for r in rows)),
                completed=len(good), failure_rate=1-len(good)/len(rows),
                timeout=sum(r["disposition"]=="timeout" for r in rows),
                max_send_delay_s=max((r.get("send_delay_s",0) for r in rows),default=0),**metrics(good),
                pods={p:dict(observed=len(x),completed=sum(r["disposition"]=="completed" for r in x),
                    rejected=sum(r["disposition"]=="rejected" for r in x),
                    clients=dict(Counter(r.get("client") for r in x)),
                    **metrics([r for r in x if r["disposition"]=="completed"])) for p,x in by_pod.items()})


def main():
    by=defaultdict(list)
    for row in lines("requests.jsonl"):
        by[row["phase"]].append(row)
    stats={k:group_stats(v) for k,v in by.items()}
    bs={k:v for k,v in stats.items() if k.startswith("B-")}
    assert len(bs)==12 and all(r["planned"]==528 for r in bs.values())
    for i in range(1,4):
        for traffic in ("uniform","skew"):
            keys=[f"B-{i}-{traffic}-{policy}" for policy in ("sticky","plain")]
            a,b=[[ (r["index"],r["offset"],r["body"]) for r in by[k] ] for k in keys]
            assert a==b,"Payload or arrival schedule changed between policies"
    labels=["uniform-sticky","uniform-plain","skew-sticky","skew-plain"]
    matches=lambda label: [bs[f"B-{i}-{label}"] for i in (1,2,3)]
    for label in labels:
        print(label, "p95:", [round(r["ttfc_s"]["p95"],3) for r in matches(label)],
              "errors:", [round(r["failure_rate"]*100,2) for r in matches(label)])
    detail=read("prom-client-window.json")
    subset=[r for r in by[detail["phase"]] if r["disposition"]=="completed" and
            detail["start"]<r["start_epoch"]+r["end_s"]<=detail["end"]]
    detail["client"]={k:quantile([r[k] for r in subset],.95) for k in ("ttfc_s","total_s")}
    status=lines("hpa-alerts.jsonl")
    t0=next(r["epoch"] for r in status if r["load"])
    large=next(r for r in status if r["pods"]>=3)
    ready3=next(r for r in status if r["ready"]>=3)
    idle=next(r for r in status if not r["load"])
    reset=next(r for r in status if r["epoch"]>ready3["epoch"] and r["pods"]==r["ready"]==2)
    cpu_samples=[x.get("resource",{}).get("current",{}).get("averageUtilization")
                 for r in status for x in r["hpa"].get("currentMetrics",[])]
    cpu_samples=[x for x in cpu_samples if isinstance(x,(int,float))]
    census=[x for x in lines("timeline.jsonl") if x["phase"]=="C-HPA-final"]
    initial={p["name"] for p in census[0]["pods"]}
    third=sorted({p["name"] for x in census for p in x["pods"]}-initial)
    alert_states={}
    for r in status:
        for a in r["alerts"]:
            alarm=alert_states.setdefault(a["labels"]["alertname"],{})
            alarm.setdefault(a["state"],r["epoch"]-t0)
    summary=dict(stats=stats,prom_comparison=detail,hpa=dict(pod3_seconds=large["epoch"]-t0,
        ready3_seconds=ready3["epoch"]-t0,back_to_2_after_idle_seconds=reset["epoch"]-idle["epoch"],
        max_observed_cpu_percent=max(cpu_samples,default=None),
        new_pods=third,
        alert_first_seconds=alert_states,alerts_after=status[-1]["alerts"]))
    (OUT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    make_charts(by,stats,status,t0,labels,matches)


def make_charts(by,stats,status,t0,labels,matches):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":11,"axes.labelcolor":"#292329",
                         "xtick.color":"#40313b","ytick.color":"#40313b","text.color":"#292329",
                         "axes.titleweight":"bold","figure.facecolor":"#faf8f6"})
    purple="#77415f"; amber="#bb793b"; green="#416344"
    def save(fig,n):
        fig.tight_layout()
        fig.savefig(OUT/f"{n}.png",dpi=140)
        plt.close(fig)

    a=lines("timeline.jsonl")
    fig,axs=plt.subplots(3,1,figsize=(10,8),sharey=True)
    fixed_phase="A-fixed-gated"
    for ax,phase,title in zip(axs,("A-defect",fixed_phase,"A-rollout-final"),
            ("A · Дефект: health вместо ready","A · Исправление: трафик только после прогрева","A · Штатное обновление A → B")):
        time_rows=[r for r in a if r["phase"]==phase]
        first=time_rows[0]["epoch"]
        x=[r["epoch"]-first for r in time_rows]
        ax.step(x,[sum(p["ready"] for p in r["pods"]) for r in time_rows],where="post",c=purple,label="Pod Ready")
        ax.step(x,[sum(e.get("ready") is True for e in r["endpoints"]) for r in time_rows],where="post",c=green,linestyle="--",label="Готовые Service endpoints")
        errors=[r for r in by[phase] if r.get("status")==503]
        if errors: ax.scatter([r["start_epoch"]+r["sent_s"]-first for r in errors],[.15]*len(errors),
                              marker="x",c=amber,label=f"HTTP 503: {len(errors)}")
        ax.set(ylabel="Реплики",title=title,ylim=(-.15,3.25),yticks=[0,1,2,3])
        ax.grid(axis="y",alpha=.2);ax.legend(loc="lower right",fontsize=9)
    axs[-1].set_xlabel("Секунды от начала фазы")
    save(fig,"01-rollout")

    fig,ax=plt.subplots(figsize=(10,5))
    x=0
    ticks=[]; names=[]
    endpoints=set.union(*(set(stats[f"B-1-skew-{v}"]["pods"]) for v in ("plain","sticky")))
    legend=False
    for variant in ("sticky","plain"):
        for pod in sorted(endpoints):
            m=stats[f"B-1-skew-{variant}"]["pods"].get(pod)
            ticks.append(x)
            if not m or not m["completed"]:
                names.append(f"{variant}\n…{pod.split('-')[-1]}\nнет выборки")
                x+=1
                continue
            names.append(f"{variant}\n…{pod.split('-')[-1]}\nn={m['completed']}")
            ax.bar(x-.18,m["queue_s"]["p95"],.34,color=amber,label="Очередь p95" if not legend else None)
            ax.bar(x+.18,m["ttfc_s"]["p95"],.34,color=purple,label="TTFC p95" if not legend else None)
            legend=True
            x+=1
    ax.set(xticks=ticks,xticklabels=names,ylabel="Секунды",title="B · Первый прогон, 80% с одного IP, успешные запросы")
    ax.legend();ax.grid(axis="y",alpha=.2)
    save(fig,"02-by-pod")

    fig,axs=plt.subplots(2,1,figsize=(10,7),sharex=True)
    for j,label in enumerate(labels):
        for i,m in enumerate(matches(label)):
            x=j+(i-1)*.15
            color=[purple,amber,green][i]
            axs[0].scatter(x,m["ttfc_s"]["p95"],c=color,s=55,label=f"Прогон {i+1}" if j==0 else None)
            axs[1].scatter(x,m["failure_rate"]*100,c=color,s=55)
    axs[0].set(title="B · 3 × 120 с на комбинацию, один backend и 4,4 запроса/с",ylabel="TTFC p95, с");axs[0].legend()
    axs[1].set(ylabel="Неуспешные, %",xticks=range(4),xticklabels=["Ровно / IP","Ровно / None","80% / IP","80% / None"])
    for ax in axs: ax.grid(axis="y",alpha=.2)
    save(fig,"03-policies")

    fig,ax=plt.subplots(figsize=(10,5))
    xs=[r["epoch"]-t0 for r in status]
    ax.step(xs,[r["pods"] for r in status],where="post",c=purple,label="Созданные Pod")
    ax.step(xs,[r["ready"] for r in status],where="post",c=green,linestyle="--",label="Ready")
    ax.set(xlabel="Секунды от начала CPU-опыта",ylabel="Реплики",yticks=[0,1,2,3],ylim=(0,3.5),
           title="C · HPA 2 → 3 → 2; target CPU 75%, min=2 max=3")
    other=ax.twinx()
    other.fill_between(xs,[10 if r["load"] else 0 for r in status],step="post",color=amber,alpha=.18,label="Вход, 10 req/s")
    other.set(ylabel="Запланированный вход, req/s",ylim=(0,14))
    ax.legend(loc="upper left");other.legend(loc="upper right")
    save(fig,"04-hpa")


if __name__=="__main__":
    main()
