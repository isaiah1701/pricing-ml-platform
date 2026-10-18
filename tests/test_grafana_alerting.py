"""Validate Grafana-managed rules and secure optional notification provisioning."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import yaml

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def test_rules_are_provisioned_once_and_have_bounded_identity() -> None:
    """Eight managed alerts use Prometheus, stable labels, and explicit missing-data policy."""
    source = (ROOT / 'monitoring/grafana/alert-rules.yml').read_text()
    assert source == (ROOT / 'serving/helm/files/grafana-alert-rules.yml').read_text()
    rules = yaml.safe_load(source)['groups'][0]['rules']
    assert {rule['title'] for rule in rules} == {'InferenceDown', 'HighInferenceErrorRate', 'HighInferenceLatency', 'ModelDriftDetected', 'DriftMonitoringStale', 'AirflowPipelineFailure', 'ModelRefreshFailure', 'CanaryRolloutFailure'}
    for rule in rules:
        assert rule['data'][0]['datasourceUid'] == 'prometheus'
        assert set(rule['labels']) == {'severity', 'service', 'environment'}
        assert rule['annotations']['runbook_url'].startswith('https://github.com/')
        assert rule['noDataState'] == ('Alerting' if rule['title'] in {'InferenceDown', 'DriftMonitoringStale'} else 'OK')
        if rule['title'] in {'HighInferenceLatency', 'HighInferenceErrorRate'}:
            assert '>= 1' in rule['data'][0]['model']['expr']
    old_rules = yaml.safe_load((ROOT / 'monitoring/prometheus/airbnb-alerts.yml').read_text())['groups'][0]['rules']
    assert not {rule['alert'] for rule in old_rules} & {'AirbnbFeatureDriftDetected', 'AirbnbServingUnavailable', 'AirbnbPredictionDriftDetected', 'AirbnbDriftMonitoringStale', 'AirbnbServingErrorRateHigh', 'AirbnbPredictionLatencyP95High'}


def test_email_notifications_require_explicit_reviewed_configuration() -> None:
    """No SMTP credentials enter Git; configured email references a real external secret."""
    command = ['helm', 'template', 'airbnb', str(ROOT / 'serving/helm'), '-f', str(ROOT / 'serving/helm/values/production.yaml'), '--set', 'platform.enabled=true']
    default = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    default_objects = list(yaml.safe_load_all(default))
    config = next(obj for obj in default_objects if obj and obj['metadata']['name'] == 'mlops-grafana-alerting')
    assert 'mlops-notifications.yaml' not in config['data']
    enabled = subprocess.run(command + ['--set', 'platform.grafanaAlerting.notifications.enabled=true', '--set', 'platform.grafanaAlerting.notifications.email=ops@example.test', '--set', 'platform.grafanaAlerting.notifications.smtpSecretArn=existing-smtp-secret'], check=True, capture_output=True, text=True).stdout
    objects = list(yaml.safe_load_all(enabled))
    config = next(obj for obj in objects if obj and obj['metadata']['name'] == 'mlops-grafana-alerting')
    notification = yaml.safe_load(config['data']['mlops-notifications.yaml'])
    assert notification['policies'][0]['receiver'] == notification['contactPoints'][0]['name']
    assert notification['policies'][0]['repeat_interval'] == '7d'
    secret = next(obj for obj in objects if obj and obj['kind'] == 'ExternalSecret' and obj['metadata']['name'] == 'grafana-smtp')
    assert secret['spec']['dataFrom'][0]['extract']['key'] == 'existing-smtp-secret'
    assert 'GF_SMTP_PASSWORD' in secret['spec']['target']['template']['data']
