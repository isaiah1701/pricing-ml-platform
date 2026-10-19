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

## Task 3 — HTTPS domain configuration (prepared)

- Reuse the existing Traefik NLB and external-dns ownership for argocd.platf0rm.site
  and airflow.platf0rm.site. No Terraform change or new load balancer is required.
- Platform chart provisions namespace-local ACME issuers/certificates and TLS routes.
  Argo has a priority gRPC/h2c route plus HTTP route; Airflow keeps FAB authentication.
- Helmfile-owned Argo values set the public URL and explicitly disable anonymous access;
  existing admin secret is preserved. Airflow API base URL uses the public HTTPS origin.
- AppProject includes argocd as an allowed destination for the certificate and route.
- Production platform/bootstrap Helm rendering and lint pass. Live DNS, certificate,
  authenticated API and CLI verification pending deployment.
- Argo ingress reference: https://argo-cd.readthedocs.io/en/latest/operator-manual/ingress/

## Current verification — 2026-10-19

- Git: `a5de46adb76eda583eb3ae76e1bc12a38ee58627`; remote `origin/main` agrees.
  Unrelated worktree changes remain preserved: `README.md`, `load-tests/`, and
  `skills/`.
- EKS `prod-mlops-prod` is ACTIVE in `eu-west-2`, Kubernetes 1.35, with one
  Ready worker. All observed workload pods are Ready/Running.
- Argo bootstrap, platform, and all platform controllers are Synced/Healthy.
  `airbnb-platform` is Synced/Healthy at this SHA. `airbnb-inference` is
  OutOfSync/Degraded because its revision-2 canary analysis correctly failed
  closed with fewer than the required 20 requests in the two-minute window;
  stable ReplicaSet `6c99449f77` remains serving and Ready. No direct patch,
  rollback, traffic generation, or analysis bypass was performed.
- Git declares inference image `sha256:b08f50e626a540e32d914fc6bbc1d8782658122319871c464ac4e872420fcb31`
  and training/Airflow/MLflow image `sha256:6cebdf6dd19036cca1fb3571486b5f0fd86d17e9cffbf30bbd00577a1af59add`.
  The healthy stable inference pod still runs
  the prior approved digest `sha256:a8b2424134fb989760344bc1241f9d9f9c15cf3a264890ffc126b04203e9ca3c`.
- Prometheus has 18/18 active targets UP. `KubeSchedulerDown`,
  `KubeControllerManagerDown`, and `KubeEtcdDown` are absent from both
  Prometheus and Alertmanager. The obsolete control-plane monitoring objects
  were pruned while worker, API-server, application, and node monitoring were
  preserved.
- All four Grafana dashboard ConfigMaps are provisioned. Every dashboard
  query executed successfully against live Prometheus: infrastructure 17
  queries/15 non-empty, business 15/12, model/drift 20/9, and pipeline 11/6.
  Empty results represent documented awaiting-source-data panels, not fake
  zeroes.
- The S3-to-Airflow-to-Pushgateway-to-Prometheus path is live: Pushgateway
  exposes current drift/data-quality metrics and is scraped UP; Airflow is
  reachable over HTTPS and retains authentication. Airflow's API requires its
  configured authenticated session/JWT rather than unauthenticated access.
- `argocd.platf0rm.site` and `airflow.platf0rm.site` resolve to the existing
  Traefik NLB and return HTTPS 200 pages. Argo CD CLI login over the public
  gRPC/HTTPS route succeeded. Both public certificates are Ready and issued by
  the existing Let's Encrypt issuer.
- Live MLflow contains registered model `airbnb-price-predictor`, concrete
  version `1`, READY status, run
  `5835ce099b55490c8adb39a67da0f887`, model source
  `models:/m-2b73fff990074bd4834961862f8a22b1`, and alias `champion -> 1`.
  Serving `/ready` and `airbnb_loaded_model_info` both report concrete version
  `1`. Existing registry/promotion code supports additive versions and guarded
  promotion/rollback; no alias or model version was changed.

### Outstanding blocker and exact resumption

The only live deployment blocker is the fail-closed inference canary: the
analysis run `airbnb-inference-547dccd8d7-2-2` failed because the canary did
not receive the minimum 20 real requests. Keep stable traffic in place. After
approved real canary traffic is available, inspect the AnalysisRun and
Prometheus samples, then reconcile only through GitOps:

```sh
kubectl -n airbnb-inference get rollout,analysisrun,pods
GITHUB_SHA=$(git rev-parse HEAD) GITHUB_REPOSITORY=isaiah1701/prod-mlops-platform \
  ARGO_APPLICATION=airbnb-inference uv run python -m scripts.sync_gitops
```

If the canary fails with real traffic, review the immutable image and revert
the application change through the normal reviewed Git/CI path before syncing.
Do not patch the Rollout, weaken minimum-request or failure conditions, run
production load merely to force approval, change `champion`, or delete the
failed AnalysisRun as a workaround.
