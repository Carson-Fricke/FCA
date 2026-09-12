export function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function readString(source: Record<string, unknown>, key: string): string | null {
	const value = source[key];
	return typeof value === 'string' && value.length > 0 ? value : null;
}

export function readNumber(source: Record<string, unknown>, key: string): number | null {
	const value = source[key];
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function readArray<T>(source: Record<string, unknown>, key: string): T[] {
	const value = source[key];
	return Array.isArray(value) ? (value as T[]) : [];
}

export function readRecord(source: Record<string, unknown>, key: string): Record<string, unknown> {
	const value = source[key];
	return isRecord(value) ? value : {};
}

export function formatValue(value: unknown): string {
	if (value === null || value === undefined) return '—';
	if (typeof value === 'string') return value;
	if (typeof value === 'number' || typeof value === 'boolean') return String(value);
	return JSON.stringify(value);
}

export function prettyJson(value: unknown): string {
	try {
		return JSON.stringify(value ?? {}, null, 2);
	} catch {
		return String(value);
	}
}
