/** Thin fetch wrapper. Every call returns parsed JSON or throws with the
 *  server's own error message, so callers never see a bare "Failed to fetch". */

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch (cause) {
    throw new ApiError('Could not reach the server. Is it still running?', 'network');
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // fall through: a non-JSON body means the server errored unexpectedly
  }

  if (!response.ok) {
    throw new ApiError(data?.detail || `Request failed (${response.status})`, 'http', response.status);
  }
  return data;
}

export class ApiError extends Error {
  constructor(message, kind, status = null) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

export const api = {
  curriculum: () => request('/api/curriculum'),
  lesson: (id) => request(`/api/lessons/${encodeURIComponent(id)}`),
  exercise: (id) => request(`/api/exercises/${encodeURIComponent(id)}`),
  hint: (id, index) => request(`/api/exercises/${encodeURIComponent(id)}/hints/${index}`),
  progress: () => request('/api/progress'),
  review: () => request('/api/review'),
  resetProgress: () => request('/api/progress', { method: 'DELETE' }),

  run: (id, files) =>
    request(`/api/exercises/${encodeURIComponent(id)}/run`, {
      method: 'POST',
      body: JSON.stringify({ files }),
    }),
  submit: (id, files) =>
    request(`/api/exercises/${encodeURIComponent(id)}/submit`, {
      method: 'POST',
      body: JSON.stringify({ files }),
    }),
  reveal: (id) => request(`/api/exercises/${encodeURIComponent(id)}/solution`, { method: 'POST' }),
  playground: (dockerfile, files) =>
    request('/api/playground/run', {
      method: 'POST',
      body: JSON.stringify({ dockerfile, files }),
    }),
};
