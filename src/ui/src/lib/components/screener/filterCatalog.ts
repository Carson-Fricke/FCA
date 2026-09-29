/**
 * Screener filter catalog: typed builder rows over the frozen screening SDK.
 *
 * The vocabulary is deliberately restricted to metrics a generated
 * `ScreeningProgram` can actually produce from its `ScreenContext`:
 *
 *  - classification and fundamentals facts through `ctx.facts(symbol)`
 *    (the engine fact-sheet keys the screening data source serves:
 *    `security_type`, `exchange`, `sic_code`/`sic_industry`, and the
 *    `shares_outstanding`/`revenue`/`net_income`/`eps` fundamentals);
 *  - bar conditions over the symbol's `ctx.bars(symbol)` lookback window
 *    (last close, mean dollar volume, SMA overlay, momentum, RSI).
 *
 * Cross-sectional percentile ranks (the legacy Python presets) are not
 * offered: the Rust SDK hands an authored program no candidate-set rank
 * surface, so a percentile row would have to fabricate its value. The
 * generated fragments read only `ctx.universe()`, `ctx.bars()` and
 * `ctx.facts()` (plus the row's own `ScreenRow.metrics`); a missing fact or
 * bar screens the symbol out deterministically, matching the engine's
 * absent-fact semantics. The harness JIT compiler remains authoritative.
 */

import type { ScreenerBarInterval } from '$lib/types';

export type FilterGroup = 'security' | 'fundamentals' | 'liquidity' | 'technical';

export type FilterParamType =
	| 'text'
	| 'choice'
	| 'number'
	| 'signed'
	| 'positiveInt'
	| 'nonNegInt'
	| 'fraction'
	| 'percent';

export interface FilterParamField {
	/** Param key used by the fragment emitter and the row summary. */
	name: string;
	label: string;
	type: FilterParamType;
	choices?: readonly string[];
	/** Raw editing default (string form); '' means optional / unset. */
	def: string;
	optional?: boolean;
}

export type FilterSurface = 'facts' | 'bars';

export interface FilterKindSpec {
	kind: string;
	label: string;
	group: FilterGroup;
	fields: FilterParamField[];
	/** Which context surface the emitted fragment reads. */
	surface: FilterSurface;
	/** Human summary from raw (string) params, for chips and comments. */
	summary: (params: Record<string, string>) => string;
	/** Metric keys the fragment records in `ScreenRow.metrics`. */
	metrics: (params: Record<string, string>) => string[];
	/** The Rust fragment; `continue` rejects the symbol. */
	fragment: (params: Record<string, string>) => string;
}

/** One builder row: editable raw params keyed by param name. */
export interface FilterRow {
	uid: number;
	kind: string;
	params: Record<string, string | number>;
}

let uidSeq = 0;

/** Fresh row identity; module-level so rows stay unique across reloads. */
export function nextRowUid(): number {
	return ++uidSeq;
}

const fmt = (raw: string): string => {
	const n = Number(raw);
	if (Number.isFinite(n) && Math.abs(n) >= 10000 && Number.isInteger(n)) {
		return n.toLocaleString('en-US');
	}
	return raw;
};

/** Render a finite JS number as a valid Rust `f64` literal. */
function rustF64(value: number): string {
	if (Number.isInteger(value) && Math.abs(value) < 1e16) return `${value}.0`;
	return String(value);
}

function numberParam(params: Record<string, string>, key: string): number {
	return Number(params[key]);
}

function textParam(params: Record<string, string>, key: string): string {
	return (params[key] ?? '').trim();
}

/** Wrap one emitted statement block at the generator's base indentation. */
function block(lines: string[]): string {
	return ['{', ...lines.map((line) => `    ${line}`), '}'].join('\n');
}

/**
 * A classification fact matched case-insensitively (engine semantics).
 * The fact value is recorded verbatim after the symbol passes.
 */
function caseInsensitiveFact(
	factKey: string,
	expected: string,
	transform: (value: string) => string
): string {
	return block([
		`let Some(fact) = facts.get("${factKey}") else { continue; };`,
		`if ${transform('fact')} != "${expected}" { continue; }`,
		`metrics.insert("${factKey}".to_string(), fact.clone());`
	]);
}

export const FILTER_KINDS: FilterKindSpec[] = [
	// -- Security ----------------------------------------------------------
	{
		kind: 'security_type',
		label: 'Security type',
		group: 'security',
		surface: 'facts',
		fields: [
			{ name: 'value', label: 'Type', type: 'choice', choices: ['stock', 'etf'], def: 'stock' }
		],
		summary: (p) => `type = ${p.value ?? '?'}`,
		metrics: () => ['security_type'],
		fragment: (p) =>
			caseInsensitiveFact('security_type', textParam(p, 'value').toLowerCase(), (v) =>
				`${v}.trim().to_ascii_lowercase()`
			)
	},
	{
		kind: 'exchange',
		label: 'Exchange',
		group: 'security',
		surface: 'facts',
		fields: [{ name: 'value', label: 'Exchange', type: 'text', def: 'NASDAQ' }],
		summary: (p) => `exchange = ${textParam(p, 'value').toUpperCase()}`,
		metrics: () => ['exchange'],
		fragment: (p) =>
			caseInsensitiveFact('exchange', textParam(p, 'value').toUpperCase(), (v) =>
				`${v}.trim().to_ascii_uppercase()`
			)
	},
	{
		kind: 'sic_industry',
		label: 'Sector / SIC industry',
		group: 'security',
		surface: 'facts',
		fields: [
			{ name: 'code', label: 'SIC code', type: 'text', def: '', optional: true },
			{ name: 'prefix', label: 'Industry prefix', type: 'text', def: '', optional: true }
		],
		summary: (p) =>
			textParam(p, 'code') ? `sic code = ${textParam(p, 'code')}` : `sic ~ ${textParam(p, 'prefix')}`,
		metrics: (p) => [textParam(p, 'code') ? 'sic_code' : 'sic_industry'],
		fragment: (p) => {
			const code = textParam(p, 'code');
			if (code) {
				return block([
					'let Some(fact) = facts.get("sic_code") else { continue; };',
					`if fact.trim() != "${code}" { continue; }`,
					'metrics.insert("sic_code".to_string(), fact.clone());'
				]);
			}
			const prefix = textParam(p, 'prefix').toLowerCase();
			return block([
				'let Some(fact) = facts.get("sic_industry") else { continue; };',
				`if !fact.trim().to_ascii_lowercase().starts_with("${prefix}") { continue; }`,
				'metrics.insert("sic_industry".to_string(), fact.clone());'
			]);
		}
	},
	// -- Fundamentals ------------------------------------------------------
	{
		kind: 'fundamentals_min',
		label: 'Fundamental >= min',
		group: 'fundamentals',
		surface: 'facts',
		fields: [
			{
				name: 'field',
				label: 'Field',
				type: 'choice',
				choices: ['shares_outstanding', 'revenue', 'net_income', 'eps'],
				def: 'revenue'
			},
			{ name: 'min', label: 'Min', type: 'signed', def: '1000000000' }
		],
		summary: (p) => `fundamentals.${p.field ?? '?'} >= ${fmt(p.min ?? '?')}`,
		metrics: (p) => [textParam(p, 'field')],
		fragment: (p) => {
			const field = textParam(p, 'field');
			return block([
				`let Some(fact) = facts.get("${field}") else { continue; };`,
				'let Ok(number) = fact.trim().parse::<f64>() else { continue; };',
				`if !(number >= ${rustF64(numberParam(p, 'min'))}) { continue; }`,
				`metrics.insert("${field}".to_string(), fact.clone());`
			]);
		}
	},
	{
		kind: 'fundamentals_max',
		label: 'Fundamental <= max',
		group: 'fundamentals',
		surface: 'facts',
		fields: [
			{
				name: 'field',
				label: 'Field',
				type: 'choice',
				choices: ['shares_outstanding', 'revenue', 'net_income', 'eps'],
				def: 'revenue'
			},
			{ name: 'max', label: 'Max', type: 'signed', def: '100000000000' }
		],
		summary: (p) => `fundamentals.${p.field ?? '?'} <= ${fmt(p.max ?? '?')}`,
		metrics: (p) => [textParam(p, 'field')],
		fragment: (p) => {
			const field = textParam(p, 'field');
			return block([
				`let Some(fact) = facts.get("${field}") else { continue; };`,
				'let Ok(number) = fact.trim().parse::<f64>() else { continue; };',
				`if !(number <= ${rustF64(numberParam(p, 'max'))}) { continue; }`,
				`metrics.insert("${field}".to_string(), fact.clone());`
			]);
		}
	},
	// -- Volume & liquidity (computed over the fetched bar window) ----------
	{
		kind: 'avg_dollar_volume_min',
		label: 'Avg $ volume >= min',
		group: 'liquidity',
		surface: 'bars',
		fields: [{ name: 'min', label: 'Min $', type: 'number', def: '250000000' }],
		summary: (p) => `avg $vol >= ${fmt(p.min ?? '?')}`,
		metrics: () => ['avg_dollar_volume'],
		fragment: (p) =>
			block([
				'let mut dollar_volume_total = 0.0_f64;',
				'let mut dollar_volume_count = 0_u64;',
				'for entry in bars.iter() {',
				'    if let (Ok(close), Ok(volume)) = (entry.close.parse::<f64>(), entry.volume.parse::<f64>()) {',
				'        dollar_volume_total += close * volume;',
				'        dollar_volume_count += 1;',
				'    }',
				'}',
				'if dollar_volume_count == 0 { continue; }',
				'let avg_dollar_volume = dollar_volume_total / dollar_volume_count as f64;',
				`if !(avg_dollar_volume >= ${rustF64(numberParam(p, 'min'))}) { continue; }`,
				'metrics.insert("avg_dollar_volume".to_string(), format!("{avg_dollar_volume:.4}"));'
			])
	},
	{
		kind: 'avg_dollar_volume_max',
		label: 'Avg $ volume <= max',
		group: 'liquidity',
		surface: 'bars',
		fields: [{ name: 'max', label: 'Max $', type: 'number', def: '10000000000' }],
		summary: (p) => `avg $vol <= ${fmt(p.max ?? '?')}`,
		metrics: () => ['avg_dollar_volume'],
		fragment: (p) =>
			block([
				'let mut dollar_volume_total = 0.0_f64;',
				'let mut dollar_volume_count = 0_u64;',
				'for entry in bars.iter() {',
				'    if let (Ok(close), Ok(volume)) = (entry.close.parse::<f64>(), entry.volume.parse::<f64>()) {',
				'        dollar_volume_total += close * volume;',
				'        dollar_volume_count += 1;',
				'    }',
				'}',
				'if dollar_volume_count == 0 { continue; }',
				'let avg_dollar_volume = dollar_volume_total / dollar_volume_count as f64;',
				`if !(avg_dollar_volume <= ${rustF64(numberParam(p, 'max'))}) { continue; }`,
				'metrics.insert("avg_dollar_volume".to_string(), format!("{avg_dollar_volume:.4}"));'
			])
	},
	{
		kind: 'price_min',
		label: 'Price >= min',
		group: 'liquidity',
		surface: 'bars',
		fields: [{ name: 'min', label: 'Min $', type: 'number', def: '20' }],
		summary: (p) => `price >= $${p.min ?? '?'}`,
		metrics: () => ['close'],
		fragment: (p) =>
			block([
				'let Some(bar) = bars.last() else { continue; };',
				'let Ok(close) = bar.close.parse::<f64>() else { continue; };',
				`if !(close >= ${rustF64(numberParam(p, 'min'))}) { continue; }`,
				'metrics.insert("close".to_string(), bar.close.clone());'
			])
	},
	{
		kind: 'price_max',
		label: 'Price <= max',
		group: 'liquidity',
		surface: 'bars',
		fields: [{ name: 'max', label: 'Max $', type: 'number', def: '1000' }],
		summary: (p) => `price <= $${p.max ?? '?'}`,
		metrics: () => ['close'],
		fragment: (p) =>
			block([
				'let Some(bar) = bars.last() else { continue; };',
				'let Ok(close) = bar.close.parse::<f64>() else { continue; };',
				`if !(close <= ${rustF64(numberParam(p, 'max'))}) { continue; }`,
				'metrics.insert("close".to_string(), bar.close.clone());'
			])
	},
	// -- Technical (computed over the fetched bar window) -------------------
	{
		kind: 'price_above_sma',
		label: 'Price >= SMA(period)',
		group: 'technical',
		surface: 'bars',
		fields: [{ name: 'period', label: 'Period', type: 'positiveInt', def: '50' }],
		summary: (p) => `price >= SMA(${p.period ?? '?'})`,
		metrics: (p) => ['close', `ind:sma(period=${textParam(p, 'period')})`],
		fragment: (p) => {
			const period = numberParam(p, 'period');
			return block([
				'let Some(bar) = bars.last() else { continue; };',
				'let Ok(close) = bar.close.parse::<f64>() else { continue; };',
				'let mut sma_total = 0.0_f64;',
				'let mut sma_count = 0_u64;',
				`for entry in bars.iter().rev().take(${period}) {`,
				'    if let Ok(value) = entry.close.parse::<f64>() {',
				'        sma_total += value;',
				'        sma_count += 1;',
				'    }',
				'}',
				'if sma_count == 0 { continue; }',
				'let sma = sma_total / sma_count as f64;',
				'if !(close >= sma) { continue; }',
				'metrics.insert("close".to_string(), bar.close.clone());',
				`metrics.insert("ind:sma(period=${period})".to_string(), format!("{sma:.6}"));`
			]);
		}
	},
	{
		kind: 'price_below_sma',
		label: 'Price <= SMA(period)',
		group: 'technical',
		surface: 'bars',
		fields: [{ name: 'period', label: 'Period', type: 'positiveInt', def: '200' }],
		summary: (p) => `price <= SMA(${p.period ?? '?'})`,
		metrics: (p) => ['close', `ind:sma(period=${textParam(p, 'period')})`],
		fragment: (p) => {
			const period = numberParam(p, 'period');
			return block([
				'let Some(bar) = bars.last() else { continue; };',
				'let Ok(close) = bar.close.parse::<f64>() else { continue; };',
				'let mut sma_total = 0.0_f64;',
				'let mut sma_count = 0_u64;',
				`for entry in bars.iter().rev().take(${period}) {`,
				'    if let Ok(value) = entry.close.parse::<f64>() {',
				'        sma_total += value;',
				'        sma_count += 1;',
				'    }',
				'}',
				'if sma_count == 0 { continue; }',
				'let sma = sma_total / sma_count as f64;',
				'if !(close <= sma) { continue; }',
				'metrics.insert("close".to_string(), bar.close.clone());',
				`metrics.insert("ind:sma(period=${period})".to_string(), format!("{sma:.6}"));`
			]);
		}
	},
	{
		kind: 'momentum_min',
		label: 'Momentum >= min %',
		group: 'technical',
		surface: 'bars',
		fields: [
			{ name: 'period', label: 'Bars', type: 'positiveInt', def: '20' },
			{ name: 'min', label: 'Min %', type: 'signed', def: '0' }
		],
		summary: (p) => `momentum(${p.period ?? '?'}d) >= ${p.min ?? '?'}%`,
		metrics: (p) => [`momentum_${textParam(p, 'period')}d`],
		fragment: (p) => {
			const period = numberParam(p, 'period');
			return block([
				'let Some(bar) = bars.last() else { continue; };',
				'let Ok(close) = bar.close.parse::<f64>() else { continue; };',
				`if bars.len() <= ${period} { continue; }`,
				`let Ok(past) = bars[bars.len() - 1 - ${period}].close.parse::<f64>() else { continue; };`,
				'if !(past > 0.0) { continue; }',
				`let momentum_${period}d = (close / past - 1.0) * 100.0;`,
				`if !(momentum_${period}d >= ${rustF64(numberParam(p, 'min'))}) { continue; }`,
				`metrics.insert("momentum_${period}d".to_string(), format!("{momentum_${period}d:.6}"));`
			]);
		}
	},
	{
		kind: 'rsi_below',
		label: 'RSI below threshold',
		group: 'technical',
		surface: 'bars',
		fields: [
			{ name: 'period', label: 'Period', type: 'positiveInt', def: '14' },
			{ name: 'threshold', label: 'Threshold', type: 'percent', def: '30' }
		],
		summary: (p) => `RSI(${p.period ?? '?'}) <= ${p.threshold ?? '?'}`,
		metrics: (p) => [`ind:rsi(period=${textParam(p, 'period')})`],
		fragment: (p) => rsiFragment(p, '<=')
	},
	{
		kind: 'rsi_above',
		label: 'RSI above threshold',
		group: 'technical',
		surface: 'bars',
		fields: [
			{ name: 'period', label: 'Period', type: 'positiveInt', def: '14' },
			{ name: 'threshold', label: 'Threshold', type: 'percent', def: '70' }
		],
		summary: (p) => `RSI(${p.period ?? '?'}) >= ${p.threshold ?? '?'}`,
		metrics: (p) => [`ind:rsi(period=${textParam(p, 'period')})`],
		fragment: (p) => rsiFragment(p, '>=')
	}
];

/** Average gain/loss RSI over the fetched closes; deterministic pre-flight math. */
function rsiFragment(params: Record<string, string>, comparison: '<=' | '>='): string {
	const period = numberParam(params, 'period');
	const metric = `ind:rsi(period=${period})`;
	const rsi = `rsi_${period}`;
	return block([
		`if bars.len() <= ${period} { continue; }`,
		'let mut rsi_gain = 0.0_f64;',
		'let mut rsi_loss = 0.0_f64;',
		'let mut rsi_count = 0_u64;',
		`for window in bars.windows(2).rev().take(${period}) {`,
		'    if let (Ok(previous), Ok(current)) = (window[0].close.parse::<f64>(), window[1].close.parse::<f64>()) {',
		'        let change = current - previous;',
		'        if change >= 0.0 { rsi_gain += change; } else { rsi_loss -= change; }',
		'        rsi_count += 1;',
		'    }',
		'}',
		'if rsi_count == 0 { continue; }',
		`let ${rsi} = if rsi_loss == 0.0 && rsi_gain == 0.0 { 50.0 } else if rsi_loss == 0.0 { 100.0 } else {`,
		`    100.0 - 100.0 / (1.0 + (rsi_gain / rsi_count as f64) / (rsi_loss / rsi_count as f64))`,
		'};',
		`if !(${rsi} ${comparison} ${rustF64(numberParam(params, 'threshold'))}) { continue; }`,
		`metrics.insert("${metric}".to_string(), format!("{${rsi}:.6}"));`
	]);
}

export const FILTER_GROUPS: { id: FilterGroup; label: string }[] = [
	{ id: 'security', label: 'Security' },
	{ id: 'fundamentals', label: 'Fundamentals' },
	{ id: 'liquidity', label: 'Volume & liquidity' },
	{ id: 'technical', label: 'Technical' }
];

/** Default row kind: a valid, universally-applicable liquidity condition. */
export const DEFAULT_KIND = 'avg_dollar_volume_min';

export function kindSpec(kind: string): FilterKindSpec | undefined {
	return FILTER_KINDS.find((k) => k.kind === kind);
}

export function defaultParams(kind: string): Record<string, string | number> {
	const params: Record<string, string | number> = {};
	for (const field of kindSpec(kind)?.fields ?? []) params[field.name] = field.def;
	return params;
}

export function newRow(kind: string = DEFAULT_KIND): FilterRow {
	return { uid: nextRowUid(), kind, params: defaultParams(kind) };
}

/**
 * Client-side mirror of the declaration-time validation the Thesis API
 * applies before hashing a definition. Returns null when the row emits a
 * valid fragment; the harness compiler still validates the emitted source.
 */
export function validateRow(row: FilterRow): string | null {
	const spec = kindSpec(row.kind);
	if (!spec) return `unknown filter kind ${row.kind}`;
	if (row.kind === 'sic_industry') {
		const code = String(row.params.code ?? '').trim();
		const prefix = String(row.params.prefix ?? '').trim();
		if (!code === !prefix) return 'provide exactly one of SIC code or industry prefix';
		if (code && !/^\d+$/.test(code)) return 'SIC code must be a numeric string';
	}
	for (const field of spec.fields) {
		const value = row.params[field.name];
		const raw = (value === undefined || value === null ? '' : String(value)).trim();
		if (!raw) {
			if (!field.optional) return `${field.label} is required`;
			continue;
		}
		if (field.type === 'choice' && field.choices && !field.choices.includes(raw)) {
			return `${field.label} must be one of: ${field.choices.join(', ')}`;
		}
		if (field.type === 'text') {
			// The fragment embeds the value as a Rust string literal, so the
			// alphabet is restricted to what can be quoted verbatim. Choice
			// values come from the catalog's own allowlist and stay safe.
			if (!/^[A-Za-z0-9][A-Za-z0-9 .&'/-]*$/.test(raw)) {
				return `${field.label} must use letters, digits, spaces, or . & ' - /`;
			}
		}
		const n = Number(raw);
		switch (field.type) {
			case 'text':
			case 'choice':
				break;
			case 'number':
				if (!Number.isFinite(n)) return `${field.label} must be a number`;
				if (n < 0) return `${field.label} must not be negative`;
				break;
			case 'signed':
				if (!Number.isFinite(n)) return `${field.label} must be a finite number`;
				break;
			case 'positiveInt':
				if (!Number.isInteger(n) || n < 1) return `${field.label} must be a positive integer`;
				break;
			case 'nonNegInt':
				if (!Number.isInteger(n) || n < 0) return `${field.label} must be a non-negative integer`;
				break;
			case 'fraction':
				if (!Number.isFinite(n) || n < 0 || n > 1) return `${field.label} must be within [0, 1]`;
				break;
			case 'percent':
				if (!Number.isFinite(n) || n < 0 || n > 100) return `${field.label} must be within [0, 100]`;
				break;
		}
	}
	return null;
}

function stringParams(params: Record<string, string | number>): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [key, value] of Object.entries(params)) out[key] = String(value);
	return out;
}

/** Human summary of one row, for chips, comments, and assertions. */
export function describeRow(row: FilterRow): string {
	const spec = kindSpec(row.kind);
	if (!spec) {
		const entries = Object.entries(row.params ?? {});
		return entries.length ? `${row.kind}(${entries.map(([k, v]) => `${k}=${v}`).join(', ')})` : row.kind;
	}
	return spec.summary(stringParams(row.params));
}

/** The Rust fragment for one valid row, or null when the row is invalid. */
export function emitFragment(row: FilterRow): string | null {
	if (validateRow(row) !== null) return null;
	const spec = kindSpec(row.kind);
	if (!spec) return null;
	return spec.fragment(stringParams(row.params));
}

/** Valid fragments for the current rows, in declaration order. */
export function buildFragments(rows: FilterRow[]): string[] {
	return rows
		.map((row) => emitFragment(row))
		.filter((fragment): fragment is string => fragment !== null);
}

/** Metric keys the current valid rows record, in declaration order. */
export function metricsForRows(rows: FilterRow[]): string[] {
	const metrics: string[] = [];
	for (const row of rows) {
		if (validateRow(row) !== null) continue;
		for (const metric of kindSpec(row.kind)?.metrics(stringParams(row.params)) ?? []) {
			if (!metrics.includes(metric)) metrics.push(metric);
		}
	}
	return metrics;
}

/** Bar intervals the harness accepts; mirrors `BAR_INTERVALS` in Thesis. */
export const BAR_INTERVALS: readonly ScreenerBarInterval[] = ['1d', '1min'];

/**
 * Map a thrown API error to a readable message. The screener router
 * surfaces invalid definitions as 422, unknown ids as 404, an uncomposed or
 * unreachable harness as 503, and a failed harness run as 502.
 */
export function describeScreenerError(error: unknown): string {
	const message = error instanceof Error ? error.message : String(error);
	if (/\b422\b/.test(message)) {
		return 'The Thesis API rejected this definition (HTTP 422). Check the name, lookback, rank metric, bar interval, and authored source.';
	}
	if (/\b404\b/.test(message)) {
		return 'That screener definition or stored result does not exist in this workspace (HTTP 404).';
	}
	if (/\b503\b/.test(message)) {
		return 'The harness screening boundary is unavailable (HTTP 503). Compose the harness MCP boundary and retry.';
	}
	if (/\b502\b/.test(message)) {
		return 'The harness screening run failed (HTTP 502). Check the compiler diagnostics and the authored program.';
	}
	if (/Thesis API \d+/.test(message)) {
		return `The screener request failed server-side (${message}).`;
	}
	return message;
}
