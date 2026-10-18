# Production operational checkpoint

## Baseline — 2026-10-18

- Git: `289a8a1dfbe1cb5201dfe9d819fc0a62f3d17683`, branch main; remote agrees.
- Preserved pre-existing changes: README.md, load-tests/, skills/.
- AWS profile isaiahOct26, eu-west-2; cluster prod-mlops-prod ACTIVE.
- Kubernetes `/readyz?verbose`: all checks pass, including etcd.
- Argo bootstrap/platform/inference: OutOfSync, Healthy at baseline revision.
  All ten chart applications: Synced, Healthy.
- Live Airflow/MLflow training image tag: sha-838f3bcec5d73f7540ede21e36b26dc89a8e171c.
  Git declares sha-5231c9e4d25debf0834b65f7b2c95a45f70f73ad plus immutable digests.
- Git credential works; gh has no independent login. No credentials printed or committed.

## Task 1 — EKS control-plane alert correction

- Prometheus live targets: 18 targets, all UP; no scheduler/controller-manager/etcd targets.
- Scheduler EndpointSlice has no endpoints. Default ServiceMonitors exist for all three
  AWS-managed components. KubeSchedulerDown and KubeControllerManagerDown are firing.
- Disable only their scrape components and matching default rule groups in
  serving/helm/values/bootstrap.yaml. Worker, pod, API server and application monitoring remain.
- EKS direct worker discovery cannot discover these managed components; EKS also offers
  separate control-plane metric APIs, which are not configured by these ServiceMonitors.
  Reference: https://docs.aws.amazon.com/eks/latest/userguide/view-raw-metrics.html
- Deployment/live resolution: pending push, CI and Argo synchronization.
- Other observed alerts: missing production/outcome/quality source data and Alertmanager
  delivery failures. These are not suppressed by this change.

## Task 2 — Dashboard repair and runtime synchronization (in progress)

- Authenticated Grafana API confirms all four UIDs provisioned: airbnb-service-health,
  airbnb-pricing-business, airbnb-model-drift, airbnb-pipeline-health.
- Executed every dashboard query against live Prometheus. Inference/container readiness,
  resources and successful prediction count (2445) return data. No current prediction
  traffic means rate 0 and latency NaN, not a collector failure.
- Rollout metadata, serving registry refresh and pipeline series are absent in the old
  running runtime. Existing source already includes their collectors.
- Added worker node CPU/memory panels; missing values explicitly await source data.
  Kept the existing dashboard UIDs/titles. Both source and packaged JSON copies agree.
- Targeted dashboard/alert tests: 7 passed. Kubernetes and Docker CI passed for 0f720d4.
- Synced bootstrap and platform at 0f720d4 via scripts.sync_gitops; platform is starting
  the already approved digest-pinned training runtime. Monitoring prune is in progress.
- S3 has existing prediction events; retraining DAG is paused and will remain paused.
  Model-quality DAG is paused; outcomes must be checked before claiming accuracy.
