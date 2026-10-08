/* Owner: frontend member. Shared API shape: ../docs/api-contract.md */
'use strict';
const API_BASE_URL = 'http://localhost:8000';

document.getElementById('check-health').addEventListener('click', async () => {
  const button = document.getElementById('check-health');
  const status = document.getElementById('connection-status');
  button.disabled = true;
  status.textContent = 'Checking backend…';
  try {
    const response = await fetch(`${API_BASE_URL}/health`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error('Health request failed');
    const result = await response.json();
    status.textContent = result.status === 'ok' ? 'Backend connected. Chat integration is the next milestone.' : 'Unexpected health response.';
  } catch {
    status.textContent = 'Could not connect. Start the backend and serve this page at http://localhost:5500.';
  } finally {
    button.disabled = false;
  }
});
