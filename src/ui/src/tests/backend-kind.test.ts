import { describe, expect, it } from 'vitest';
import * as api from '$lib/api';
import { SAME_ORIGIN_API_BASE, apiBaseUrl, isLoopbackBase } from '$lib/api';

describe('backend base classification (local vs azure)', () => {
	it('treats loopback bases as local, not remote', () => {
		expect(isLoopbackBase('http://127.0.0.1:8000')).toBe(true);
		expect(isLoopbackBase('http://localhost:8000')).toBe(true);
		expect(isLoopbackBase('http://[::1]:8000')).toBe(true);
		expect(isLoopbackBase('127.0.0.1')).toBe(false);
		expect(isLoopbackBase('')).toBe(false);
	});

	it('does not treat a remote host as local', () => {
		expect(isLoopbackBase('https://thesis.example.com')).toBe(false);
		expect(isLoopbackBase('https://thesis.azurecontainerapps.io')).toBe(false);
	});
});

describe('same-origin API base', () => {
	it('targets the linked-backend /api prefix by default', () => {
		expect(SAME_ORIGIN_API_BASE).toBe('/api');
		expect(apiBaseUrl).toBe('/api');
	});

	it('does not expose a baked bearer credential to the browser', () => {
		expect('apiBearerToken' in api).toBe(false);
	});
});
