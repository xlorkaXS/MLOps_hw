"""Experiments from the host, load generated inside four Kubernetes Pods."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parent
os.chdir(ROOT)
OUT=ROOT/"results"
OUT.mkdir(exist_ok=True)
K=["kubectl"]
H=["helm"]
URL="http://hw11-plain:8091/generate"


def run(*args, check=True):
    p=subprocess.run(args,capture_output=True,text=True,timeout=150)
    if p.returncode and check:
        raise RuntimeError(" ".join(args)+"\n"+p.stderr+p.stdout)
    return p.stdout


def kub(*args): return run(*K,*args)
def obj(*args): return json.loads(kub(*args,"-o","json"))


def record(path,rows):
    with (OUT/path).open("a") as f:
        for row in rows:
            f.write(json.dumps(row,ensure_ascii=False)+"\n")


def change(**values):
    args=[]
    for k,v in values.items():
        args.extend(("--set",f"{k}={str(v).lower() if isinstance(v,bool) else v}"))
    return run(*H,"upgrade","--install","--reset-values","hw11","chart",*args)


def wait_ready():
    kub("rollout","status","deploy/hw11","--timeout=90s")


def wait_until(action,timeout=100):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        x=action()
        if x: return x
        time.sleep(1)
    raise RuntimeError("Timed out waiting for Kubernetes state")


def app_pods():
    return obj("get","pods","-l","app=hw11")["items"]


def snapshot(phase):
    pods=app_pods()
    es=obj("get","endpointslices","-l","kubernetes.io/service-name=hw11-plain")
    hpa=obj("get","hpa")
    return dict(phase=phase,epoch=time.time(),pods=[
        dict(name=p["metadata"]["name"], created=p["metadata"]["creationTimestamp"],
             terminating=p["metadata"].get("deletionTimestamp"),
             ready=next((c["status"]=="True" for c in p["status"].get("conditions",[]) if c["type"]=="Ready"),False),
             ready_at=next((c["lastTransitionTime"] for c in p["status"].get("conditions",[]) if c["type"]=="Ready"),None),
             guard=[s.get("restartCount",0) for s in p["status"].get("containerStatuses",[])]) for p in pods],
        endpoints=[{"name":e.get("targetRef",{}).get("name"),**e.get("conditions",{})}
                   for s in es["items"] for e in s["endpoints"]],hpa=hpa["items"])


@contextmanager
def tracking(phase):
    stop=threading.Event()
    def capture():
        while not stop.is_set():
            try: record("timeline.jsonl",[snapshot(phase)])
            except Exception as e: print("snapshot failed:",e,flush=True)
            stop.wait(1)
    t=threading.Thread(target=capture)
    t.start()
    try: yield
    finally:
        stop.set()
        t.join()


def start_load(label,policy="plain",traffic="uniform",duration=120,rate=4.4,cpu=0,gap=.10,chunks=4,clients=4):
    mark=time.time()+2.5
    procs=[subprocess.Popen([*K,"exec",f"load-{i}","--","python","/app/load.py",
           "--url",f"http://hw11-{policy}:8091/generate","--start",str(mark),"--duration",str(duration),
           "--rate",str(rate),"--profile",traffic,"--client",str(i),"--clients",str(clients),
           "--cpu",str(cpu),"--gap",str(gap),"--chunks",str(chunks)],
           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(clients)]
    drains=[]
    for p in procs:
        captured={}
        def drain(proc=p, state=captured):
            try:
                state["out"],state["err"]=proc.communicate(timeout=210)
            except subprocess.TimeoutExpired:
                proc.terminate()
                state["out"],state["err"]=proc.communicate()
        thread=threading.Thread(target=drain)
        thread.start()
        drains.append((p,thread,captured))
    return label,policy,traffic,mark,drains


def finish(group):
    label,policy,traffic,mark,procs=group
    rows=[]
    for p,thread,state in procs:
        thread.join()
        out,err=state["out"],state["err"]
        if p.returncode:
            raise RuntimeError(err)
        rows.extend(json.loads(s) for s in out.splitlines() if s.strip())
    for r in rows: r.update(phase=label,policy=policy,traffic=traffic,start_epoch=mark)
    rows.sort(key=lambda r:r["index"])
    record("requests.jsonl",rows)
    print(label,len(rows),{d:sum(r["disposition"]==d for r in rows)
                 for d in sorted({r["disposition"] for r in rows})},flush=True)
    return rows


def sample(payload, target="hw11-plain", client=0):
    code=("import json,time,load; from sys import argv; "
          "n=time.monotonic(); print(json.dumps(load.request(argv[1],{'body':json.loads(argv[2])},n,n,35)))")
    return json.loads(kub("exec",f"load-{client}","--","python","-c",code,
                         f"http://{target}:8091/generate",json.dumps(payload)))


def wait_pod_count(n):
    return wait_until(lambda: len(app_pods())==n)


def a():
    change()
    wait_ready()
    with tracking("A-defect"):
        change(prematureProbes=True)
        defect=finish(start_load("A-defect",duration=28,rate=2))
        assert any(r["status"]==503 for r in defect)
    # Force a new Pod generation: changed probe paths change the pod template.
    with tracking("A-fixed-gated"):
        change()
        fixed=finish(start_load("A-fixed-gated",duration=28,rate=2))
        wait_ready()
        assert len(fixed)==56 and all(r["disposition"]=="completed" for r in fixed)
    a_tail()


def a_tail():
    change()
    wait_ready()
    with tracking("A-rollout-final"):
        # Long stream occupies one complete slot; bound arrivals so it cannot fill all eight waiters.
        group=start_load("A-rollout-final",duration=51,rate=.35)
        executor=subprocess.Popen([*K,"exec","load-0","--","python","/app/load.py",
            "--url",URL,"--duration","1","--rate","1","--client","0","--clients","1",
            "--gap",".75","--chunks","32"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        time.sleep(2)
        update_epoch=time.time()
        change(modelVersion="B")
        wait_ready()
        long=json.loads(executor.communicate(timeout=45)[0])
        long.update(phase="A-rollout-final-long",update_epoch=update_epoch)
        record("requests.jsonl",[long])
        rows=finish(group)
        assert long['disposition']=='completed' and long['model_id']=='sim-A'
        assert {r['model_id'] for r in rows}=={'sim-A','sim-B'}
        assert all(r['disposition']=='completed' for r in rows)
    a_bad()


def a_bad():
    with tracking("A-bad-and-rollback"):
        # Restore A first; then candidate B itself never passes startup probe.
        change()
        wait_ready()
        def serving_a():
            r=sample({"prompt":"version","chunks":1})
            return r if r["model_id"]=="sim-A" and r["disposition"]=="completed" else None
        wait_until(serving_a,timeout=25)
        history=json.loads(run(*H,"history","hw11","-o","json"))
        revision=str(history[-1]["revision"])
        change(modelVersion="B",neverReady=True)
        rows=finish(start_load("A-bad-B",duration=27,rate=2))
        assert all(r["model_id"]=="sim-A" and r["disposition"]=="completed" for r in rows)
        run(*H,"rollback","hw11",revision)
        wait_ready()
        r=wait_until(serving_a,timeout=25)
        record("requests.jsonl",[dict(r,phase="A-confirm-rollback")])
    (OUT/"A-logs.txt").write_text(kub("logs","-l","app=hw11","--all-containers","--prefix","--tail=1000"))


def reset_cache():
    for p in app_pods():
        ip=p["status"]["podIP"]
        code=("import urllib.request; print(urllib.request.urlopen(urllib.request.Request("
              f"'http://{ip}:8091/internal/clear',method='POST'),timeout=3).status)")
        assert kub("exec","load-0","--","python","-c",code).strip()=="200"


def b():
    wait_ready()
    client_details=obj("get","pods","-l","app=load")
    (OUT/"client-ips.json").write_text(json.dumps({p["metadata"]["name"]:p["status"]["podIP"]
          for p in client_details["items"]},indent=2))
    for i in range(1,4):
        for traffic in ("uniform","skew"):
            for policy in ("sticky","plain"):
                label=f"B-{i}-{traffic}-{policy}"
                reset_cache()
                finish(start_load(label,policy,traffic))


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("phase",choices=["A","B","a_tail","a_bad"])
    globals()[ap.parse_args().phase.lower()]()
