#!/bin/sh
set -eu
kubectl create configmap prometheus-config --from-file=monitoring/prometheus.yml --from-file=monitoring/alerts.yml --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap grafana-source --from-file=monitoring/grafana-datasource.yml --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap grafana-provider --from-file=monitoring/grafana-dashboards.yml --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap grafana-dashboard --from-file=monitoring/dashboard.json --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f monitoring/kube.yaml
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.1/components.yaml
# kind's demo kubelet certificate is not signed by a trusted serving CA.
kubectl -n kube-system patch deployment metrics-server --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
