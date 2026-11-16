"""Forced failure, live HTTP checks, and CPU HPA/alerts experiment."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

from run import (OUT,K,change,wait_ready,app_pods,wait_until,kub,obj,run,tracking,
                 start_load,finish,record,sample)


def inside(url):
    return json.loads(kub("exec","load-0","--","python","-c",
        "import sys,urllib.request; print(urllib.request.urlopen(sys.argv[1],timeout=8).read().decode())",url))


def prom(endpoint,**kwargs):
    return inside("http://prometheus:9090/api/v1/"+endpoint+"?"+urlencode(kwargs))


def endpoint_url(pod):
    return f'{pod["status"]["podIP"]}'


def verify():
    summary={}
    # Re-run the readiness control with explicit values reset. Preserve earlier pilot traces.
    change(prematureProbes=True)
    wait_ready()
    time.sleep(21)
    with tracking("A-fixed-gated"):
        change()
        rows=finish(start_load("A-fixed-gated",duration=28,rate=2))
        wait_ready()
        assert all(r["disposition"]=="completed" for r in rows)
    with tracking("verify"):
        two=app_pods()
        victim=two[0]
        control={"prompt":"Echo the submitted history","history":["FIRST-731"],"session_id":"first","chunks":2}
        before=sample(control,endpoint_url(victim))
        assert before["observed_text"]=="FIRST-731" and before["model_id"]=="sim-A"
        name=victim["metadata"]["name"]
        kub("delete","pod",name,"--wait=false")
        known={p["metadata"]["name"] for p in two}
        new=wait_until(lambda:next((p for p in app_pods() if p["metadata"]["name"] not in known
                                   and p["status"].get("podIP")),None))
        name_new=new["metadata"]["name"]
        def unready_sample():
            value=sample(dict(control),endpoint_url(new))
            return value if value["status"]==503 else None
        probe=wait_until(unready_sample,timeout=10)
        assert all(e.get("ready") is not True for s in obj("get","endpointslices","-l","kubernetes.io/service-name=hw11-plain")["items"]
                    for e in s["endpoints"] if e.get("targetRef",{}).get("name")==name_new)
        wait_until(lambda:any(p["metadata"]["name"]==name_new and
                   any(s["ready"] for s in p["status"].get("containerStatuses",[]))
                   for p in app_pods()),timeout=60)
        after=sample(control,endpoint_url(new))
        other=sample(dict(control,history=["SECOND-982"],session_id="second"),endpoint_url(new),client=1)
        assert before["pod"]!=after["pod"] and after["model_id"]==before["model_id"]=="sim-A"
        assert after["observed_text"]=="FIRST-731" and other["observed_text"]=="SECOND-982"
        assert after["request_id"]!=other["request_id"]
        summary.update(warmup_block=probe,replacement_before=before,replacement_after=after,dialogue_other=other)
        record("requests.jsonl",[dict(r,phase="D-"+k) for k,r in summary.items()])
    with tracking("forced"):
        victim=app_pods()[0]
        before_restart=victim["status"]["containerStatuses"][0]["restartCount"]
        container=victim["status"]["containerStatuses"][0]["containerID"].split("://")[1]
        # Client prints every chunk in real time; the first chunk synchronizes termination.
        code="""import http.client,json,sys,time
host=sys.argv[1]
conn=http.client.HTTPConnection(host,8091,timeout=34)
t=time.monotonic()
conn.request('POST','/generate',json.dumps({'prompt':'force stop','chunks':32,'gap':.8}),
             {'Content-Type':'application/json','Connection':'close'})
response=conn.getresponse()
print(json.dumps({'status':response.status,'pod':response.getheader('X-Pod'),'model_id':response.getheader('X-Model-ID'),
                  'request_id':response.getheader('X-Request-ID'),'sent_monotonic':t}),flush=True)
try:
 while line:=response.readline():
  print(json.dumps(dict(json.loads(line), observed_monotonic=time.monotonic())),flush=True)
except Exception as e:
 print(json.dumps({'kind':'error','error':str(e),'observed_monotonic':time.monotonic()}),flush=True)
finally:
 print(json.dumps({'kind':'eof','observed_monotonic':time.monotonic()}),flush=True)
"""
        client=subprocess.Popen([*K,"exec","load-0","--","python","-c",code,endpoint_url(victim)],
                                 text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            headers=json.loads(client.stdout.readline())
            chunk=json.loads(client.stdout.readline())
            assert headers["status"]==200 and chunk["kind"]=="chunk"
            killed=time.time()
            run("docker","exec","hw11-control-plane","crictl","stop","--timeout","0",container)
            rest,err=client.communicate(timeout=42)
            packets=[chunk]+[json.loads(line) for line in rest.splitlines()]
            assert not any(p["kind"]=="done" for p in packets)
            forced=dict(headers,phase="D-forced",disposition="aborted",chunks_seen=sum(p["kind"]=="chunk" for p in packets),
                        first_monotonic=chunk["observed_monotonic"],end_monotonic=packets[-1]["observed_monotonic"],
                        packets=packets,killed_epoch=killed)
            record("requests.jsonl",[forced])
        finally:
            if client.poll() is None:
                client.terminate()
                client.wait(5)
        restored=wait_until(lambda:next((p for p in app_pods() if p["metadata"]["name"]==headers["pod"]
                           and p["status"]["containerStatuses"][0]["restartCount"]>before_restart
                           and p["status"]["containerStatuses"][0]["ready"]),None))
        ok=sample({"prompt":"after recovery","chunks":2},endpoint_url(restored))
        assert ok["disposition"]=="completed" and ok["model_id"]=="sim-A"
        summary.update(forced=forced,after_force=ok)
        record("requests.jsonl",[dict(ok,phase="D-after-force")])

    phases={}
    for line in (OUT/"requests.jsonl").read_text().splitlines():
        r=json.loads(line)
        phases.setdefault(r["phase"],[]).append(r)
    assert any(r.get("status")==503 for r in phases["A-defect"])
    assert all(r.get("status")!=503 for r in phases["A-fixed-gated"])
    assert phases["A-confirm-rollback"][0]["model_id"]=="sim-A"
    rejected=[r for phase in phases.values() for r in phase if r.get("status")==429]
    assert rejected and all(r["disposition"]=="rejected" and r["chunks_seen"]==0 for r in rejected)
    (OUT/"checks.json").write_text(json.dumps(dict(warmup_and_gating=True,rollback_version=True,
             replacement_version=True,accounting_abort=True,isolated_dialogues=True,evidence=summary),ensure_ascii=False,indent=2))
    print("5 live Kubernetes checks passed")


def c():
    kub("get","--raw","/apis/metrics.k8s.io/v1beta1/nodes")
    prom("targets")
    change(hpa=True)
    wait_until(lambda:obj("get","hpa","hw11").get("status",{}).get("currentMetrics"),timeout=100)
    wait_until(lambda:not prom("alerts")["data"]["alerts"], timeout=45)
    with tracking("C-HPA-final"):
        t0=time.time()
        group=start_load("C-heavy-sticky",policy="sticky",traffic="skew",duration=85,rate=10,cpu=.22)
        watch_end=time.monotonic()+180
        seen3=False
        samples=[]
        while time.monotonic()<watch_end:
            now=time.time()
            a=prom("alerts")["data"]["alerts"]
            pods=app_pods()
            hpa=obj("get","hpa","hw11")["status"]
            current=hpa.get("currentReplicas",0)
            seen3 |= sum(p["status"].get("containerStatuses",[{}])[0].get("ready",False) for p in pods)>=3
            row=dict(epoch=now,load=now<group[3]+85,ready=sum(
                p["status"].get("containerStatuses",[{}])[0].get("ready",False) for p in pods),
                pods=len(pods),hpa=hpa,alerts=a)
            record("hpa-alerts.jsonl",[row])
            samples.append(row)
            if seen3 and now>group[3]+110 and row["ready"]==row["pods"]==current==2 and not a:
                break
            time.sleep(2)
        rows=finish(group)
    states={a["labels"]["alertname"]:set() for row in samples for a in row["alerts"]}
    for row in samples:
        for alert in row["alerts"]:states[alert["labels"]["alertname"]].add(alert["state"])
    print("HPA ready:",sorted({x["ready"] for x in samples}),"alerts:",states,flush=True)
    (OUT/"hpa-final.yaml").write_text(kub("get","hpa","hw11","-o","yaml"))
    (OUT/"metrics-api.json").write_text(kub("get","--raw","/apis/metrics.k8s.io/v1beta1/pods"))
    (OUT/"prometheus-targets.json").write_text(json.dumps(prom("targets"),indent=2))
    dash=inside("http://grafana:3000/api/dashboards/uid/hw11")
    (OUT/"grafana-export.json").write_text(json.dumps(dash["dashboard"],indent=2))
    assert seen3 and samples[-1]["ready"]==2
    assert all(states.get(a,set()) >= {"pending","firing"} for a in ("SlowFirstChunk","TooManyRefusals"))
    assert not samples[-1]["alerts"]


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("phase",choices=["verify","c"])
    globals()[p.parse_args().phase]()
