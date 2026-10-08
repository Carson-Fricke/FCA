/** Signed/monetary/percent formatting for numerics (JetBrains Mono surfaces).
 *
 * A ``null`` value means the harness or the registry did not publish the figure;
 * it renders as a dash, never as a zero.
 */

export function signed(value: number | null, digits = 1): string {
	if (value == null) return '—';
	return `${value >= 0 ? '+' : '-'}${Math.abs(value).toFixed(digits)}`;
}

export function signedPct(value: number | null, digits = 1): string {
	if (value == null) return '—';
	return `${signed(value, digits)}%`;
}

export function money(value: number | null, digits = 0): string {
	if (value == null) return '—';
	return `${value < 0 ? '-' : ''}$${Math.abs(value).toLocaleString('en-US', {
		minimumFractionDigits: digits,
		maximumFractionDigits: digits
	})}`;
}

export function pct(value: number | null, digits = 1): string {
	if (value == null) return '—';
	return `${(value * 100).toFixed(digits)}%`;
}

export function relativeTime(iso: string): string {
	const then = new Date(iso).getTime();
	const mins = Math.max(1, Math.floor((Date.now() - then) / 60000));
	if (mins < 60) return `${mins}m ago`;
	if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
	return `${Math.floor(mins / 1440)}d ago`;
}

export function shortDate(iso: string): string {
	return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: '2-digit' });
}

export function shortHash(hash: string | null): string {
	return hash ? `${hash.slice(0, 12)}…` : '—';
}
