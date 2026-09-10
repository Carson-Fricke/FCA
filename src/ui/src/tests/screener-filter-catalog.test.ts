import { describe, expect, it } from 'vitest';
import {
	DEFAULT_KIND,
	buildFilters,
	describeScreenerError,
	describeSpec,
	newRow,
	validateRow,
	specToRow,
	rowToSpec
} from '$lib/components/screener/filterCatalog';
import type { FilterRow } from '$lib/components/screener/filterCatalog';
import { SCREEN_TEMPLATE, lintScreenScript } from '$lib/components/screener/screenScript';
import type { ScreenerFilterSpec } from '$lib/types';

function rowOf(kind: string, params: Record<string, string>): FilterRow {
	return { uid: 1, kind, params };
}

describe('Filter catalog: payload correctness', () => {
	it('builds engine-native filter specs from edited rows', () => {
		const rows = [
			rowOf('security_type', { value: 'etf' }),
			rowOf('fundamentals_min', { field: 'revenue', min: '1000000000' }),
			rowOf('rsi_below', { period: '14', threshold: '30' }),
			rowOf('avg_dollar_volume_min', { min: '250000000' }),
			rowOf('dollar_volume_percentile_min', { min: '0.5', window_days: '' })
		];
		expect(buildFilters(rows)).toEqual([
			{ kind: 'security_type', params: { value: 'etf' } },
			{ kind: 'fundamentals_min', params: { field: 'revenue', min: 1000000000 } },
			{ kind: 'rsi_below', params: { period: 14, threshold: 30 } },
			{ kind: 'avg_dollar_volume_min', params: { min: 250000000 } },
			{ kind: 'dollar_volume_percentile_min', params: { min: 0.5 } }
		] satisfies ScreenerFilterSpec[]);
	});

	it('drops invalid rows from the payload while keeping valid ones', () => {
		const rows = [
			rowOf('rsi_above', { period: '14', threshold: '150' }),
			rowOf('min_price', { min: '5' })
		];
		expect(buildFilters(rows)).toEqual([{ kind: 'min_price', params: { min: 5 } }]);
	});

	it('keeps optional params out of the payload when unset', () => {
		const spec = rowToSpec(rowOf('dollar_volume_percentile_min', { min: '0.8', window_days: ' ' }));
		expect(spec).toEqual({ kind: 'dollar_volume_percentile_min', params: { min: 0.8 } });
	});

	it('round-trips a saved definition through row hydration', () => {
		const saved: ScreenerFilterSpec = {
			kind: 'fundamentals_max',
			params: { field: 'net_income', max: 250000000 }
		};
		const row = specToRow(saved);
		expect(row.params).toEqual({ field: 'net_income', max: '250000000' });
		expect(rowToSpec(row)).toEqual(saved);
	});

	it('hydrates missing params from catalog defaults for known kinds', () => {
		const row = specToRow({ kind: 'rsi_below', params: { threshold: 42 } });
		expect(row.params).toEqual({ period: '14', threshold: '42' });
	});

	it('carries unknown kinds and marks them invalid', () => {
		const row = specToRow({ kind: 'from_the_future', params: { x: 1 } });
		expect(row.kind).toBe('from_the_future');
		expect(row.params).toEqual({ x: '1' });
		expect(validateRow(row)).toContain('unknown filter kind');
	});
});

describe('Filter catalog: declaration-time validation (FilterError mirror)', () => {
	it('rejects fractions outside [0, 1]', () => {
		expect(validateRow(rowOf('atr_percentile_min', { period: '14', min: '1.5' }))).
			toBe('Min pct must be within [0, 1]');
	});

	it('rejects RSI thresholds outside [0, 100]', () => {
		expect(validateRow(rowOf('rsi_below', { period: '14', threshold: '101' }))).
			toBe('Threshold must be within [0, 100]');
	});

	it('rejects non-positive periods', () => {
		expect(validateRow(rowOf('price_above_sma', { period: '0' }))).
			toBe('Period must be a positive integer');
	});

	it('rejects negative liquidity bounds', () => {
		expect(validateRow(rowOf('avg_dollar_volume_min', { min: '-1' }))).
			toBe('Min $ must not be negative');
	});

	it('allows negative fundamentals bounds (net income)', () => {
		expect(validateRow(rowOf('fundamentals_min', { field: 'net_income', min: '-5000000' }))).
			toBeNull();
	});

	it('requires exactly one SIC selector', () => {
		expect(validateRow(rowOf('sic_industry', { code: '', prefix: '' }))).
			toBe('provide exactly one of SIC code or industry prefix');
		expect(validateRow(rowOf('sic_industry', { code: '20', prefix: 'food' }))).
			toBe('provide exactly one of SIC code or industry prefix');
		expect(validateRow(rowOf('sic_industry', { code: '2O', prefix: '' }))).
			toBe('SIC code must be a numeric string');
		expect(validateRow(rowOf('sic_industry', { code: '2090', prefix: '' }))).
			toBeNull();
	});

	it('requires non-empty exchange values', () => {
		expect(validateRow(rowOf('exchange', { value: '' }))).toBe('Exchange is required');
	});
});

describe('Filter catalog: summaries and error mapping', () => {
	it('summarizes known and unknown kinds for saved-study chips', () => {
		expect(describeSpec({ kind: 'rsi_below', params: { period: 14, threshold: 30 } })).toBe(
			'RSI(14) ≤ 30'
		);
		expect(describeSpec({ kind: 'from_the_future', params: { x: 1 } })).toBe(
			'from_the_future(x=1)'
		);
	});

	it('maps screening HTTP failures to readable messages', () => {
		expect(describeScreenerError(new Error('Thesis API 422 on /v1/workspaces/ws/screening/run'))).
			toContain('FilterError');
		expect(describeScreenerError(new Error('Thesis API 503 on /v1/workspaces/ws/screening'))).
			toContain('data source is not configured');
		expect(describeScreenerError(new Error('Thesis API 404 on /v1/workspaces/ws/screening/x'))).
			toContain('does not exist');
		expect(describeScreenerError(new Error('boom'))).toBe('boom');
	});
});

describe('Custom screen script: boundary feedback', () => {
	it('passes the default screen(ctx) template', () => {
		expect(lintScreenScript(SCREEN_TEMPLATE)).toEqual([]);
	});

	it('requires a non-empty source and one screen function', () => {
		expect(lintScreenScript('')).toEqual([
			{ line: null, message: 'screener source must be a non-empty string', severity: 'error' }
		]);
		const noScreen = lintScreenScript('def other(ctx):\n    return []\n');
		const noScreenMessages = noScreen.map((issue) => issue.message);
		expect(noScreenMessages).toContain('the module must define a screen(ctx) function');
		expect(
			lintScreenScript('def screen(ctx):\n    return []\ndef screen2(ctx):\n    return []\n'
		).some((issue) => issue.message.includes('exactly one screen(ctx)'))).toBe(true);
	});

	it('flags unbalanced brackets', () => {
		const issues = lintScreenScript('def screen(ctx):\n    return [{"symbol": 1\n');
		expect(issues.some((issue) => issue.message.startsWith('unbalanced'))).toBe(true);
	});

	it('enforces the import allowlist and rejects aliases', () => {
		const issues = lintScreenScript(
			'import os as o\nfrom collections import defaultdict\ndef screen(ctx):\n    return []\n'
		);
		const messages = issues.map((issue) => issue.message);
		expect(messages).toContain("import of 'os' is outside the allowlist (math, statistics)");
		expect(messages).toContain('import aliases are not permitted in custom screeners');
		expect(messages).toContain(
			"import from 'collections' is outside the allowlist (math, statistics)"
		);
	});

	it('rejects other top-level statements and dunder attribute access', () => {
		const issues = lintScreenScript(
			'THRESHOLD = 5\ndef screen(ctx):\n    return ctx.__class__\n'
		);
		const messages = issues.map((issue) => issue.message);
		expect(messages.some((message) => message.startsWith('top-level statement'))).toBe(true);
		expect(messages).toContain('dunder attribute access is not permitted in custom screeners');
	});

	it('warns when screen(ctx) never returns', () => {
		const issues = lintScreenScript('def screen(ctx):\n    pass\n');
		expect(issues).toEqual([
			{ line: null, message: 'screen(ctx) must return a list of dicts', severity: 'warning' }
		]);
	});

	it('rows start from a valid default kind', () => {
		expect(validateRow(newRow(DEFAULT_KIND))).toBeNull();
	});
});
