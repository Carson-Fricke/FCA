import { describe, expect, it } from 'vitest';
import {
	DEFAULT_KIND,
	FILTER_KINDS,
	buildFragments,
	describeRow,
	describeScreenerError,
	emitFragment,
	metricsForRows,
	newRow,
	validateRow
} from '$lib/components/screener/filterCatalog';
import type { FilterRow } from '$lib/components/screener/filterCatalog';

function rowOf(kind: string, params: Record<string, string>): FilterRow {
	return { uid: 1, kind, params };
}

describe('Filter catalog: fragment mapping over the SDK surface', () => {
	it('maps security and fundamentals rows to fact-sheet reads', () => {
		const security = emitFragment(rowOf('security_type', { value: 'etf' })) ?? '';
		expect(security).toContain('facts.get("security_type")');
		expect(security).toContain('to_ascii_lowercase()');
		expect(security).toContain('!= "etf"');
		expect(security).toContain('metrics.insert("security_type".to_string()');

		const exchange = emitFragment(rowOf('exchange', { value: 'nasdaq' })) ?? '';
		expect(exchange).toContain('facts.get("exchange")');
		expect(exchange).toContain('!= "NASDAQ"');

		const sicCode = emitFragment(rowOf('sic_industry', { code: '2090', prefix: '' })) ?? '';
		expect(sicCode).toContain('facts.get("sic_code")');
		expect(sicCode).toContain('!= "2090"');

		const sicPrefix = emitFragment(rowOf('sic_industry', { code: '', prefix: 'Food' })) ?? '';
		expect(sicPrefix).toContain('facts.get("sic_industry")');
		expect(sicPrefix).toContain('starts_with("food")');

		const fundamentals = emitFragment(
			rowOf('fundamentals_min', { field: 'revenue', min: '1000000000' })
		) ?? '';
		expect(fundamentals).toContain('facts.get("revenue")');
		expect(fundamentals).toContain('parse::<f64>()');
		expect(fundamentals).toContain('number >= 1000000000.0');
	});

	it('maps liquidity and technical rows to bar computations', () => {
		const dollarVolume = emitFragment(
			rowOf('avg_dollar_volume_min', { min: '250000000' })
		) ?? '';
		expect(dollarVolume).toContain('for entry in bars.iter()');
		expect(dollarVolume).toContain('entry.volume.parse::<f64>()');
		expect(dollarVolume).toContain('avg_dollar_volume >= 250000000.0');
		expect(dollarVolume).toContain('metrics.insert("avg_dollar_volume"');

		const price = emitFragment(rowOf('price_min', { min: '20' })) ?? '';
		expect(price).toContain('bars.last()');
		expect(price).toContain('bar.close.parse::<f64>()');
		expect(price).toContain('close >= 20.0');

		const sma = emitFragment(rowOf('price_above_sma', { period: '50' })) ?? '';
		expect(sma).toContain('bars.iter().rev().take(50)');
		expect(sma).toContain('ind:sma(period=50)');

		const momentum = emitFragment(rowOf('momentum_min', { period: '20', min: '5' })) ?? '';
		expect(momentum).toContain('bars.len() <= 20');
		expect(momentum).toContain('momentum_20d >= 5.0');
		expect(momentum).toContain('metrics.insert("momentum_20d"');

		const rsi = emitFragment(rowOf('rsi_below', { period: '14', threshold: '30' })) ?? '';
		expect(rsi).toContain('bars.windows(2).rev().take(14)');
		expect(rsi).toContain('ind:rsi(period=14)');
		expect(rsi).toContain('rsi_14 <= 30.0');
	});

	it('never emits outside the frozen authoring surface', () => {
		for (const spec of FILTER_KINDS) {
			const params: Record<string, string> = {};
			for (const field of spec.fields) params[field.name] = field.def;
			if (spec.kind === 'sic_industry') params.code = '2090';
			const fragment = spec.fragment(params);
			expect(fragment, spec.kind).toMatch(/facts\.get\(|bars/);
			expect(fragment, spec.kind).not.toMatch(
				/ctx\.indicators|ctx\.orders|ctx\.parameters|std::fs|std::net|SystemTime|TcpStream/
			);
		}
	});

	it('builds fragments only for valid rows, in declaration order', () => {
		const rows = [
			rowOf('security_type', { value: 'etf' }),
			rowOf('fundamentals_min', { field: 'revenue', min: 'not-a-number' }),
			rowOf('price_min', { min: '20' })
		];
		const fragments = buildFragments(rows);
		expect(fragments).toHaveLength(2);
		expect(fragments[0]).toContain('security_type');
		expect(fragments[1]).toContain('close >= 20.0');
	});

	it('reports metric keys per row and maps unknown kinds to null', () => {
		expect(metricsForRows([rowOf('rsi_above', { period: '21', threshold: '70' })])).toEqual([
			'ind:rsi(period=21)'
		]);
		expect(metricsForRows([rowOf('price_above_sma', { period: '50' })])).toEqual([
			'close',
			'ind:sma(period=50)'
		]);
		expect(metricsForRows([rowOf('fundamentals_max', { field: 'eps', max: '10' })])).toEqual([
			'eps'
		]);
		expect(emitFragment(rowOf('from_the_future', { x: '1' }))).toBeNull();
		expect(describeRow(rowOf('from_the_future', { x: '1' }))).toBe('from_the_future(x=1)');
	});
});

describe('Filter catalog: declaration-time validation', () => {
	it('rejects RSI thresholds outside [0, 100]', () => {
		expect(validateRow(rowOf('rsi_below', { period: '14', threshold: '101' }))).toBe(
			'Threshold must be within [0, 100]'
		);
	});

	it('rejects non-positive periods', () => {
		expect(validateRow(rowOf('price_above_sma', { period: '0' }))).toBe(
			'Period must be a positive integer'
		);
	});

	it('rejects negative liquidity bounds but allows negative fundamentals', () => {
		expect(validateRow(rowOf('avg_dollar_volume_min', { min: '-1' }))).toBe(
			'Min $ must not be negative'
		);
		expect(validateRow(rowOf('fundamentals_min', { field: 'net_income', min: '-5000000' }))).toBeNull();
	});

	it('requires exactly one SIC selector', () => {
		expect(validateRow(rowOf('sic_industry', { code: '', prefix: '' }))).toBe(
			'provide exactly one of SIC code or industry prefix'
		);
		expect(validateRow(rowOf('sic_industry', { code: '20', prefix: 'food' }))).toBe(
			'provide exactly one of SIC code or industry prefix'
		);
		expect(validateRow(rowOf('sic_industry', { code: '2O', prefix: '' }))).toBe(
			'SIC code must be a numeric string'
		);
		expect(validateRow(rowOf('sic_industry', { code: '2090', prefix: '' }))).toBeNull();
	});

	it('keeps embedded string literals quote-safe', () => {
		expect(validateRow(rowOf('exchange', { value: 'NASDAQ' }))).toBeNull();
		expect(validateRow(rowOf('exchange', { value: 'BAD"QUOTE' }))).toBe(
			"Exchange must use letters, digits, spaces, or . & ' - /"
		);
		expect(validateRow(rowOf('exchange', { value: '' }))).toBe('Exchange is required');
	});

	it('starts rows from a valid default kind', () => {
		expect(validateRow(newRow(DEFAULT_KIND))).toBeNull();
	});
});

describe('Filter catalog: summaries and error mapping', () => {
	it('summarizes rows for chips and comments', () => {
		expect(describeRow(rowOf('rsi_below', { period: '14', threshold: '30' }))).toBe('RSI(14) <= 30');
		expect(describeRow(rowOf('avg_dollar_volume_min', { min: '250000000' }))).toBe(
			'avg $vol >= 250,000,000'
		);
	});

	it('maps screener HTTP failures to readable messages', () => {
		expect(describeScreenerError(new Error('Thesis API 422 on /v1/workspaces/ws/screeners'))).toContain(
			'rejected this definition'
		);
		expect(describeScreenerError(new Error('Thesis API 503 on /v1/workspaces/ws/screeners/run'))).toContain(
			'harness screening boundary is unavailable'
		);
		expect(describeScreenerError(new Error('Thesis API 502 on /v1/workspaces/ws/screeners/run'))).toContain(
			'run failed'
		);
		expect(describeScreenerError(new Error('Thesis API 404 on /v1/workspaces/ws/screeners/results/x'))).toContain(
			'does not exist'
		);
		expect(describeScreenerError(new Error('boom'))).toBe('boom');
	});
});
