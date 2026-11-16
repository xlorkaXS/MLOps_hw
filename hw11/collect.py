"""Read-only evidence export while the kind cluster is alive."""
import json
import time
from collections import defaultdict

from operate import prom
from run import OUT,kub,run


def main():
    for name, args in [
        ('kubernetes-version.json',['version','-o','json']),
        ('services.yaml',['get','svc','hw11-plain','hw11-sticky','-o','yaml']),
        ('deployment.yaml',['get','deploy','hw11','-o','yaml']),
        ('pods-final.yaml',['get','pods','-o','wide']),
        ('endpoints-final.yaml',['get','endpointslices','-o','yaml']),
        ('events.txt',['get','events','--sort-by=.metadata.creationTimestamp']),
        ('top.txt',['top','pods','--containers'])]:
        (OUT/name).write_text(kub(*args))
    for name in ('prometheus','grafana','kube-state'):
        (OUT/(name+'-image.txt')).write_text(kub("get","pod","-l",f"app={name}",
            "-o","jsonpath={.items[0].status.containerStatuses[0].imageID}"))
    (OUT/"kind-images.txt").write_text(run("docker","exec","hw11-control-plane","crictl","images"))
    (OUT/"resources-after.txt").write_text(run("docker","system","df")+"\n"+run("docker","exec",
          "hw11-control-plane","df","-h","/")+run("docker","exec","hw11-control-plane","df","-h","/var/lib/containerd"))
    (OUT/"api-image.json").write_text(run("docker","image","inspect","hw11-api:1.0.0","--format",
            '{"id":"{{.Id}}","size":{{.Size}},"architecture":"{{.Architecture}}"}'))
    (OUT/"kubeproxy.txt").write_text(kub("get","configmap","kube-proxy","-n","kube-system","-o","yaml"))
    server=kub("logs","-l","app=hw11","--prefix","--tail=200")
    (OUT/"C-server.log").write_text(server)
    groups=defaultdict(list)
    for line in (OUT/"requests.jsonl").read_text().splitlines():
        r=json.loads(line)
        if r["phase"].startswith("B-"): groups[r["phase"]].append(r)
    # Interior of a completed comparison window, avoiding queue spill-over at the edges.
    label="B-1-uniform-plain"
    rows=groups[label]
    epoch=rows[0]["start_epoch"]
    start,end=epoch+12,epoch+108
    complete=[r for r in rows if r["disposition"]=="completed" and start<epoch+r["end_s"]<=end]
    promql=("histogram_quantile(0.95,sum by(le)(increase(lab_ttfc_seconds_bucket[96s])))")
    expressions={"ttfc_p95":promql,"count":"sum(increase(lab_ttfc_seconds_count[96s]))",
                 "total_p95":"histogram_quantile(0.95,sum by(le)(increase(lab_total_seconds_bucket[96s])))"}
    info=dict(phase=label,start=start,end=end,n_client=len(complete),
              server={k:dict(expr=v,result=prom("query",query=v,time=end)) for k,v in expressions.items()})
    (OUT/"prom-client-window.json").write_text(json.dumps(info,indent=2))
    epoch=groups["B-1-skew-sticky"][0]["start_epoch"]
    for key in ("running","waiting"):
        result=prom("query_range",query="lab_"+key,start=epoch,end=epoch+120,step="3")
        (OUT/("prom-skew-"+key+".json")).write_text(json.dumps(result))
    for key, expr in {
        "cpu":'sum by(pod)(rate(container_cpu_usage_seconds_total{container="api",pod=~"hw11-.*"}[15s]))',
        "ram":'sum by(pod)(container_memory_working_set_bytes{container="api",pod=~"hw11-.*"})',
        "ready":'kube_deployment_status_replicas_ready{deployment="hw11"}',
        "waiting":"lab_waiting","running":"lab_running",
    }.items():
        now=time.time()
        data=prom("query_range",query=expr,start=now-240,end=now,step="3")
        (OUT/("prom-"+key+".json")).write_text(json.dumps(data))
    print("Cluster, images, Prometheus histogram and Grafana data exported.")


if __name__=="__main__":
    main()
