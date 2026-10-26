// Only the host transport is substituted. Routes, pairing, pagination and bytes
// come from the actual Python API, and the production NetworkSource is unchanged.
let origin = '';
export function setOrigin(value: string): void { origin = value; }
export async function requestUrl(options: { url: string; method: string; headers: Record<string, string>; contentType?: string; body?: string }) {
  const url = new URL(options.url);
  if (url.origin !== 'https://fixture.invalid' || !origin.startsWith('http://127.0.0.1:')) throw new Error('Not a test URL');
  const response = await fetch(origin + url.pathname + url.search, { method: options.method, body: options.body,
    headers: { ...options.headers, ...(options.contentType ? { 'Content-Type': options.contentType } : {}) }, signal: AbortSignal.timeout(10000) });
  const arrayBuffer = await response.arrayBuffer();
  return { status: response.status, arrayBuffer, get json() { return JSON.parse(new TextDecoder().decode(arrayBuffer)); } };
}
