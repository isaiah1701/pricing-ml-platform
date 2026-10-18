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
