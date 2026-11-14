import http from 'k6/http';
import { check } from 'k6';
export const options = { vus: 1, iterations: 3 };
export default function () {
  check(http.get(`${__ENV.BASE_URL || 'http://localhost:8787'}/health`), { 'health 200': r => r.status === 200 });
}
