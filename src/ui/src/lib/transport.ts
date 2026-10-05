/** HTTP mechanics only; endpoint clients own wire decoding and presentation adapters. */
import { decodeWire, WireDecodeError } from './wire';
export class ThesisApiError extends Error {
	constructor(
		readonly status: number,
		readonly path: string,
		readonly code: string | null = null,
		message?: string,
		readonly details: unknown = null
	) {
		super(message || `Thesis API ${status} on ${path}`);
		this.name = 'ThesisApiError';
	}
}

export async function fetchJson<T>(baseUrl: string, path: string, init?: RequestInit): Promise<T> {
	const headers = new Headers(init?.headers);
	if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
	const response = await fetch(`${baseUrl}${path}`, { ...init, headers });
	if (!response.ok) {
		let payload: unknown = null;
		try { payload = await response.json(); } catch { /* Proxies may return HTML or an empty body. */ }
		const envelope = payload && typeof payload === 'object' ? payload as Record<string, unknown> : null;
		if (envelope && 'error' in envelope) {
			try {
				const { error } = decodeWire('ErrorResponse', payload, path);
				throw new ThesisApiError(response.status, path, error.code, error.message, error.details ?? null);
			} catch (error) {
				if (error instanceof ThesisApiError) throw error;
				throw new ThesisApiError(response.status, path, 'invalid_error_response', undefined, payload);
			}
		}
		const detail = envelope?.detail ?? envelope;
		const error = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail as Record<string, unknown> : null;
		throw new ThesisApiError(response.status, path,
			typeof error?.code === 'string' ? error.code : null,
			typeof error?.message === 'string' ? error.message : typeof detail === 'string' ? detail : undefined,
			error?.details ?? detail);
	}
	if (response.status === 204) return undefined as T;
	const text = await response.text();
	try { return (text ? JSON.parse(text) : undefined) as T; }
	catch { throw new WireDecodeError('JSON', path, 'Response body is not valid JSON'); }
}
