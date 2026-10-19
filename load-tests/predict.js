import http from 'k6/http';
import { check, fail, sleep } from 'k6';

const baseUrl = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/+$/, '');
const listings = JSON.parse(open('./listings.json'));

export const options = {
  vus: 100,
  duration: '5m',
  thresholds: {
    'http_req_failed{endpoint:predict}': ['rate<0.05'],
    'http_req_duration{endpoint:predict}': ['p(95)<250'],
    checks: ['rate>0.95'],
  },
};

export function setup() {
  const response = http.get(`${baseUrl}/ready`, { timeout: '10s' });
  if (!check(response, { 'model is ready': (r) => r.status === 200 })) {
    fail('Model is not ready; check BASE_URL and the deployed champion.');
  }
}

export default function () {
  const listing = listings[(__VU + __ITER - 1) % listings.length];
  const response = http.post(`${baseUrl}/predict`, JSON.stringify(listing), {
    headers: { 'Content-Type': 'application/json' },
    tags: { endpoint: 'predict' },
    timeout: '10s',
  });
  let prediction = null;
  if (response.status === 200) {
    try {
      prediction = response.json();
    } catch (_) {
      // A malformed successful response must fail the checks below.
    }
  }
  check(response, {
    'prediction returns 200': (r) => r.status === 200,
    'price is finite and non-negative': () => prediction !== null
      && Number.isFinite(prediction.predicted_price) && prediction.predicted_price >= 0,
    'prediction has identity and model version': () => prediction !== null
      && typeof prediction.prediction_id === 'string' && prediction.prediction_id.length > 0
      && typeof prediction.model_version === 'string' && prediction.model_version.length > 0,
  });
  sleep(1);
}
