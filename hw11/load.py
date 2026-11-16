"""Open-loop HTTP NDJSON load generator. Monotonic request timing, independent arrival clock."""
import argparse
import concurrent.futures
import http.client
import json
import socket
import time
from urllib.parse import urlsplit


def request(url, item, scheduled, origin, timeout):
    target = urlsplit(url)
    now = time.monotonic()
    result = dict(item, scheduled_monotonic=scheduled, scheduled_s=scheduled-origin,
                  sent_monotonic=None, sent_s=None, first_monotonic=None,end_monotonic=None,
                  status=None, pod=None, model_id=None, request_id=None,
                  chunks_seen=0, chunk_times=[], disposition="not_sent")
    if now - scheduled > .75:
        result["disposition"]="expired"
        return result
    conn = http.client.HTTPConnection(target.hostname,target.port or 80,timeout=timeout)
    try:
        sent = time.monotonic()
        result.update(sent_s=sent-origin,sent_monotonic=sent,send_delay_s=sent-scheduled)
        conn.request("POST",target.path or "/generate",json.dumps(item["body"]).encode(),
                     {"Content-Type":"application/json","Connection":"close"})
        response = conn.getresponse()
        result.update(status=response.status,pod=response.getheader("X-Pod"),
                      model_id=response.getheader("X-Model-ID"), request_id=response.getheader("X-Request-ID"))
        if response.status != 200:
            body = json.loads(response.read())
            result.update(disposition="rejected",pod=body.get("pod"),model_id=body.get("model_id"),
                          request_id=body.get("request_id"))
            return result
        result["disposition"]="aborted"
        while line := response.readline():
            packet = json.loads(line)
            if packet["kind"] == "done":
                if result["chunks_seen"] == item["body"]["chunks"] == packet["chunks"]:
                    result.update(disposition="completed",server_seconds=packet["total_seconds"])
                break
            stamp = time.monotonic()
            if not result["chunks_seen"]:
                result.update(first_monotonic=stamp,ttfc_s=stamp-sent,
                              pod=packet["pod"],model_id=packet["model_id"],request_id=packet["request_id"],
                              backend_mode=packet["backend_mode"],queue_s=packet["queue_seconds"],
                              cache_hit=packet["cache_hit"],observed_text=packet["text"])
            result["chunk_times"].append(stamp-sent)
            result["chunks_seen"]+=1
    except (socket.timeout, TimeoutError) as e:
        result.update(disposition="timeout",error=str(e))
    except (OSError, http.client.HTTPException, ValueError) as e:
        result.update(disposition="aborted" if result["status"]==200 else "transport_error",error=str(e))
    finally:
        end = time.monotonic()
        result.update(end_monotonic=end,end_s=end-origin,total_s=end-(result["sent_monotonic"] or end))
        conn.close()
    return result


def schedule(profile,duration,rate,client,clients,cpu,gap,chunks):
    for i in range(int(duration*rate)):
        owner = i%clients if profile=="uniform" else 0 if i%5<4 else 1+(i//5)%(clients-1)
        if client == owner:
            yield dict(index=i,client=client,profile=profile,offset=i/rate,
                       body=dict(prompt="Повтори контрольную строку", history=[f"SESSION-{i%17:02d}"],
                                 prefix=f"dialog-{client}",session_id=f"c{client}-{i%17}",
                                 chunks=chunks,gap=gap,cpu_seconds=cpu))


def main():
    p=argparse.ArgumentParser()
    for k,t,d in [('duration',float,120),('rate',float,4.4),('client',int,0),('clients',int,4),
                  ('start',float,0),('cpu',float,0),('gap',float,.10),('chunks',int,4),('timeout',float,35)]:
        p.add_argument('--'+k,type=t,default=d)
    p.add_argument('--profile',choices=['uniform','skew'],default='uniform')
    p.add_argument('--url',required=True)
    a=p.parse_args()
    origin=time.monotonic()+max(0,a.start-time.time())
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        futures=[]
        for row in schedule(a.profile,a.duration,a.rate,a.client,a.clients,a.cpu,a.gap,a.chunks):
            tick=origin+row["offset"]
            time.sleep(max(0,tick-time.monotonic()))
            futures.append(pool.submit(request,a.url,row,tick,origin,a.timeout))
        for f in concurrent.futures.as_completed(futures):
            print(json.dumps(f.result(),ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
