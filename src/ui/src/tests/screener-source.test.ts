import { describe, expect, it } from 'vitest';
import { FILTER_KINDS, validateRow } from '$lib/components/screener/filterCatalog';
import type { FilterRow } from '$lib/components/screener/filterCatalog';
import {
	ROWS_MARKER,
	SCREENER_TEMPLATE,
	generateScreenerSource,
	lintScreenerSource,
	parseFilterRows
} from '$lib/components/screener/screenerSource';

function rowOf(uid: number, kind: string, params: Record<string, string>): FilterRow {
	return { uid, kind, params };
}

const ROWS: FilterRow[] = [
	rowOf(1, 'avg_dollar_volume_min', { min: '250000000' }),
	rowOf(2, 'price_min', { min: '20' })
];

describe('Screener source generation', () => {
	it('generates one deterministic export_screening_program! program', () => {
		const source = generateScreenerSource(ROWS);
		expect(generateScreenerSource(ROWS)).toBe(source);
		expect(source).toContain('export_screening_program!(UiScreener);');
		expect(source).toContain('impl ScreeningProgram for UiScreener {');
		expect(source).toContain('fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow>');
		expect(source).toContain('for symbol in ctx.universe().iter()');
		expect(source).toContain('ctx.bars(symbol)');
		expect(source).not.toContain('ctx.facts(symbol)');
		expect(source).toContain('// filter: avg $vol >= 250,000,000');
		expect(source).toContain('// filter: price >= $20');
	});

	it('adds the facts binding only when a fact row is present', () => {
		const facts = generateScreenerSource([rowOf(1, 'security_type', { value: 'etf' })]);
		expect(facts).toContain('let facts = ctx.facts(symbol);');
		expect(facts).toContain('facts.get("security_type")');
	});

	it('round-trips builder rows through the marker comment', () => {
		const source = generateScreenerSource(ROWS);
		const parsed = parseFilterRows(source);
		expect(parsed?.map((row) => ({ kind: row.kind, params: row.params }))).toEqual([
			{ kind: 'avg_dollar_volume_min', params: { min: '250000000' } },
			{ kind: 'price_min', params: { min: '20' } }
		]);
		expect(source.split('\n').some((line) => line.startsWith(ROWS_MARKER))).toBe(true);
	});

	it('skips invalid rows and returns null when no marker is present', () => {
		const source = generateScreenerSource([
			rowOf(1, 'rsi_below', { period: '14', threshold: '150' }),
			rowOf(2, 'price_min', { min: '20' })
		]);
		expect(source).not.toContain('rsi_14');
		expect(source).toContain('close >= 20.0');
		expect(parseFilterRows('fn screen() {}')).toBeNull();
		expect(parseFilterRows(`${ROWS_MARKER}not json`)).toBeNull();
	});

	it('renders the no-filter template as a valid close screen', () => {
		expect(generateScreenerSource([])).toBe(SCREENER_TEMPLATE);
		expect(lintScreenerSource(SCREENER_TEMPLATE)).toEqual([]);
		expect(SCREENER_TEMPLATE).toContain('metrics.insert("close".to_string()');
	});
});

describe('Screener source pre-flight', () => {
	it('passes the generated source for every catalog kind', () => {
		const rows = FILTER_KINDS.map((spec, index) => {
			const params: Record<string, string> = {};
			for (const field of spec.fields) params[field.name] = field.def;
			if (spec.kind === 'sic_industry') params.code = '2090';
			const row: FilterRow = { uid: index + 1, kind: spec.kind, params };
			expect(validateRow(row), spec.kind).toBeNull();
			return row;
		});
		const issues = lintScreenerSource(generateScreenerSource(rows));
		expect(issues.filter((issue) => issue.severity === 'error')).toEqual([]);
	});

	it('requires non-empty source and the two authoring pieces', () => {
		expect(lintScreenerSource('')).toEqual([
			{ line: null, message: 'screener source must be a non-empty string', severity: 'error' }
		]);
		const messages = lintScreenerSource('struct Only {}').map((issue) => issue.message);
		expect(messages).toContain('the program must define `fn screen(&mut self, ctx: &ScreenContext)`');
		expect(messages).toContain(
			'the source must export the program with `export_screening_program!(...)`'
		);
	});

	it('flags unbalanced brackets but ignores comments and literals', () => {
		const unbalanced = lintScreenerSource(
			'fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow> {\n'
		);
		expect(unbalanced[0]).toEqual({
			line: 1,
			message: "unbalanced '{' — check bracket pairing from this line",
			severity: 'error'
		});
		const balanced = [
			'// a comment with { unbalanced',
			'fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow> {',
			'    let text = format!("{value:.4}");',
			'    /* block { comment } */',
			'    Vec::new()',
			'}',
			'export_screening_program!(UiScreener);'
		].join('\n');
		expect(lintScreenerSource(balanced)).toEqual([]);
	});
});
