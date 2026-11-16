"""Submission checks, offline, using the preserved raw Kubernetes evidence."""
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

root=Path(__file__).resolve().parent
out=root/"results"
trace=[json.loads(r) for r in (out/"requests.jsonl").read_text().splitlines()]
phases=defaultdict(list)
for row in trace:
    phases[row["phase"]].append(row)
summary=json.loads((out/"summary.json").read_text())
live=json.loads((out/"checks.json").read_text())
assert len({ip for ip in json.loads((out/"client-ips.json").read_text()).values()})==4
assert sum(r["status"]==503 for r in phases["A-defect"])>0
assert all(r["disposition"]=="completed" for r in phases["A-fixed-gated"])
assert all(r["disposition"]=="completed" and r["status"]==200 for r in phases["A-rollout-final"])
a=phases["A-rollout-final-long"][0]
assert (a["model_id"],a["disposition"],a["chunks_seen"])==("sim-A","completed",32)
assert {r["model_id"] for r in phases["A-rollout-final"]}=={"sim-A","sim-B"}
assert all(r["model_id"]=="sim-A" and r["disposition"]=="completed" for r in phases["A-bad-B"])
assert phases["A-confirm-rollback"][0]["model_id"]=="sim-A"
assert phases["D-forced"][0]["disposition"]=="aborted" and not any(
    e["kind"]=="done" for e in phases["D-forced"][0]["packets"])
assert all(v is True for k,v in live.items() if k!="evidence")
report=(root/"report.html").read_bytes()
for i in range(1,5):
    plot=list(out.glob(f"0{i}-*.png"))
    assert len(plot)==1 and plot[0].stat().st_size>1000
    assert plot[0].name.encode() in report
for i in range(1,4):
    for traffic in ("uniform","skew"):
        schedules=[]
        for policy in ("plain","sticky"):
            rows=phases[f"B-{i}-{traffic}-{policy}"]
            assert len(rows)==528 and max(r["offset"] for r in rows)>119.5
            schedules.append([(r["index"],r["offset"],r["body"]) for r in rows])
            assert set(r.get("backend_mode") for r in rows if r["disposition"]=="completed")=={"simulated"}
            assert all(r["chunks_seen"]==r["body"]["chunks"] for r in rows if r["disposition"]=="completed")
        assert schedules[0]==schedules[1]
hpa=summary["hpa"]
assert len(phases["C-heavy-sticky"])==850
assert hpa["ready3_seconds"]>hpa["pod3_seconds"]
assert hpa["back_to_2_after_idle_seconds"]>0 and not hpa["alerts_after"]
for alert in ("SlowFirstChunk","TooManyRefusals"):
    states=hpa["alert_first_seconds"][alert]
    assert states["firing"]>states["pending"]
p=json.loads((out/"prometheus-targets.json").read_text())["data"]["activeTargets"]
timeline=[json.loads(row) for row in (out/"timeline.jsonl").read_text().splitlines()]
last_c=next(r for r in reversed(timeline) if r["phase"]=="C-HPA-final")
ready_pods={pod["name"] for pod in last_c["pods"] if pod["ready"] and not pod["terminating"]}
scraped_pods={t["labels"]["pod"] for t in p if t["labels"]["job"]=="api" and t["health"]=="up"}
assert len(ready_pods)==2 and ready_pods <= scraped_pods
assert {t["labels"]["job"] for t in p if t["health"]=="up"}>={"api","cadvisor","state"}
dash=json.loads((out/"grafana-export.json").read_text())
assert dash["uid"]=="hw11" and len(dash["panels"])==8
print("PASS: probes, rollout, rollback, forced failure, confinement, 12 schedules, HPA, 2 alerts, all charts.")
print("Requests:",len(trace),"report sha256:",hashlib.sha256(report).hexdigest())
