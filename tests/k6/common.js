import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const listings = new SharedArray('validated NYC listing samples', () => JSON.parse(open('./listings.json')));
const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
if (!/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/.test(baseUrl) && __ENV.ALLOW_REMOTE !== 'true') {
  throw new Error('Remote targets require ALLOW_REMOTE=true and explicit operator approval.');
}

const errors = new Rate('prediction_errors');
const completed = new Counter('predictions');
const latency = new Trend('prediction_latency', true);
const recoveryLatency = new Trend('recovery_latency', true);
const recoveryErrors = new Rate('recovery_errors');
const p95 = Number(__ENV.P95_MS || 1000);
const errorRate = Number(__ENV.MAX_ERROR_RATE || 0.05);
const minimumRate = Number(__ENV.MIN_RPS || 0);
const stageSeconds = Number(__ENV.STAGE_SECONDS || 10);
if (!(p95 > 0) || !(errorRate > 0 && errorRate < 1) || !(minimumRate >= 0)) {
  throw new Error('P95_MS > 0, 0 < MAX_ERROR_RATE < 1 and MIN_RPS >= 0 are required.');
}
if (!Number.isInteger(stageSeconds) || stageSeconds < 1) {
  throw new Error('STAGE_SECONDS must be a positive integer.');
}

export function makeOptions(mode) {
  const thresholds = {
    prediction_errors: [`rate<${errorRate}`],
    prediction_latency: [`p(95)<${p95}`],
    predictions: ['count>0', `rate>=${minimumRate}`],
    checks: ['rate>0.95'],
  };
  let scenarios;
  if (mode === 'smoke') {
    scenarios = { smoke: { executor: 'constant-vus', vus: 1, duration: __ENV.DURATION || '10s' } };
  } else if (mode === 'baseline') {
    scenarios = { baseline: { executor: 'constant-vus', vus: Number(__ENV.VUS || 100), duration: __ENV.DURATION || '5m' } };
  } else if (mode === 'burst') {
    scenarios = {
      burst: { executor: 'ramping-vus', startVUs: 5, stages: [
        { duration: `${2 * stageSeconds}s`, target: 5 },
        { duration: `${stageSeconds}s`, target: Number(__ENV.VUS || 100) },
        { duration: `${3 * stageSeconds}s`, target: Number(__ENV.VUS || 100) },
        { duration: `${stageSeconds}s`, target: 5 },
        { duration: `${6 * stageSeconds}s`, target: 5 },
      ] },
    };
    thresholds.recovery_latency = [`p(95)<${p95}`];
    thresholds.recovery_errors = [`rate<${errorRate}`];
    thresholds['predictions{phase:recovery}'] = ['count>0'];
  } else {
    throw new Error(`Unknown test mode: ${mode}`);
  }
  return { scenarios, thresholds, summaryTrendStats: ['avg', 'p(90)', 'p(95)', 'max'] };
}

export function setupCheck() {
  const response = http.get(`${baseUrl}/ready`, { timeout: '10s' });
  if (!check(response, { 'model is ready': (r) => r.status === 200 })) {
    fail('Target is not ready; check the model and BASE_URL.');
  }
}

export function predict() {
  // Burst recovery starts after the 70-second warmup/ramp/high-load/ramp-down sequence.
  const phase = exec.scenario.name === 'burst' && exec.instance.currentTestRunDuration >= 7 * stageSeconds * 1000
    ? 'recovery' : 'load';
  const listing = listings[(__VU + __ITER - 1) % listings.length];
  const response = http.post(`${baseUrl}/predict`, JSON.stringify(listing), {
    headers: { 'Content-Type': 'application/json' },
    tags: { endpoint: 'predict', phase }, timeout: '10s',
  });
  let body = null;
  try { body = response.json(); } catch (_) { /* Count malformed responses as failures. */ }
  const valid = check(response, {
    'prediction returns 200': (r) => r.status === 200,
    'price is finite and non-negative': () => body !== null
      && Number.isFinite(body.predicted_price) && body.predicted_price >= 0,
    'prediction identity and version are present': () => body !== null
      && typeof body.prediction_id === 'string' && body.prediction_id.length > 0
      && typeof body.model_version === 'string' && body.model_version.length > 0,
  });
  completed.add(1, { phase });
  errors.add(!valid);
  latency.add(response.timings.duration);
  if (phase === 'recovery') {
    recoveryErrors.add(!valid);
    recoveryLatency.add(response.timings.duration);
  }
  sleep(1);
}
