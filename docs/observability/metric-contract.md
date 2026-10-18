# V1 observability contract

Prometheus is Grafana's only operational datasource. S3 retains detailed drift
reports and recoverable latest batch metric snapshots. Airflow publishes completed
batch results with fixed job/grouping keys; no run IDs, request IDs, customer IDs
or raw features are metric labels. Existing `airbnb_` names remain canonical.

- Serving: existing HTTP counters, prediction latency histogram, readiness and
  loaded model info; add `airbnb_model_refresh_failures_total`,
  `airbnb_model_refresh_last_success_timestamp_seconds` and
  `airbnb_champion_model_info{model,alias,version}` and
  `airbnb_registry_available` (latest alias lookup; unknown before observation). Model info gauges replace
  previous version series rather than accumulating them.
- Drift: retain existing `airbnb_dataset_drift`, `airbnb_drifted_features`,
  `airbnb_feature_drift_score{feature,kind,model_version}` and
  `airbnb_drift_last_success_timestamp_seconds`. Add `airbnb_drift_share`,
  `airbnb_drift_threshold` (dataset share threshold),
  `airbnb_feature_drift_threshold` and `airbnb_drift_evaluation_failures_total`.
  Counts are gauges. Success timestamps reflect completed S3 report publication.
- Pipelines: `airbnb_pipeline_last_run_success{dag}`,
  `airbnb_pipeline_last_success_timestamp_seconds{dag}`,
  `airbnb_pipeline_last_duration_seconds{dag}`,
  `airbnb_pipeline_failures_total{dag}`. DAG labels are restricted to existing
  platform DAGs; failed retries must not erase the last successful timestamp.
- Offline quality: `airbnb_offline_evaluation_mae{role}`,
  `airbnb_offline_evaluation_rmse{role}`,
  `airbnb_offline_evaluation_r2{role}` with role champion/challenger only. These
  describe evaluation holdouts, never live production accuracy. Existing delayed
  outcome metrics remain unavailable when matching ground truth is absent.
- Model lifecycle: `airbnb_model_promotions_total`,
  `airbnb_model_rollbacks_total`, `airbnb_model_rollback_failures_total`,
  `airbnb_model_last_promotion_timestamp_seconds`,
  `airbnb_model_last_rollback_timestamp_seconds`. Events carry audit IDs in
  durable state, never labels. A rollback success means serving was verified at
  the previous version; alias restoration alone is insufficient.
- Kubernetes: existing kube-state-metrics/cAdvisor series; add explicitly
  configured Argo Rollouts custom-resource metrics for Rollout/AnalysisRun state.
  `mlops_rollout_info{namespace,rollout,phase,revision}`,
  `mlops_rollout_stable_info{namespace,rollout,revision}`, and
  `mlops_analysis_run_info{namespace,analysisrun,phase}` are Info gauges from
  actual Rollout/AnalysisRun status fields. `mlops_rollout_aborted` exposes
  the actual Rollout status.abort flag as a gauge, with absent optional flags
  treated as false for an existing Rollout. KSM exposes only the pod template hash
  label needed to join stable/canary traffic. Do not query assumed controller
  metrics that aren't exposed.

Dashboards deduplicate stable/canary ServiceMonitor scrapes per pod before
aggregation. Alert identities exclude evaluation timestamps and model versions;
notification grouping/repeat policy prevents repeated notifications on each scrape.
No data, stale success and a healthy non-breaching evaluation are distinct states.
