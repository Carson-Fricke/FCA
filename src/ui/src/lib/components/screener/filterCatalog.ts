/**
 * Screener filter catalog: the engine-native preset vocabulary
 * (`src/thesis/screening/presets.py`) mirrored as typed editor metadata.
 *
 * Every kind declaration carries the parameter fields the engine validates
 * at declaration time, so the builder's client-side validation mirrors the
 * backend FilterError semantics (required numbers, [0,1] fractions, [0,100]
 * RSI thresholds, positive-int periods, exactly-one SIC selector) and the
 * emitted payloads are engine-valid without a round trip.
 */

import type { ScreenerFilterSpec } from '$lib/types';

export type FilterGroup = 'security' | 'fundamentals' | 'liquidity' | 'technical';

export interface FilterParamField {
	/** Param key sent to the engine (matches presets.py validators). */
	name: string;
	label: string;
	type:
		| 'text'
		| 'choice'
		| 'number'
		| 'signed'
		| 'positiveInt'
		| 'nonNegInt'
		| 'fraction'
		| 'percent';
	choices?: readonly string[];
	/** Raw editing default (string form); '' means optional / unset. */
	def: string;
	optional?: boolean;
}

export interface FilterKindSpec {
	kind: string;
	label: string;
	group: FilterGroup;
	fields: FilterParamField[];
	/** Human summary from raw (string) params, for chips and saved lists. */
	summary: (params: Record<string, string>) => string;
}

/** One builder row: editable raw params keyed by param name (number
 * inputs bind numbers; the catalog coerces defensively). */
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

export const FILTER_KINDS: FilterKindSpec[] = [
	// -- Security ----------------------------------------------------------
	{
		kind: 'security_type',
		label: 'Security type',
		group: 'security',
		fields: [{ name: 'value', label: 'Type', type: 'choice', choices: ['stock', 'etf'], def: 'stock' }],
		summary: (p) => `type = ${p.value ?? '?'}`
	},
	{
		kind: 'exchange',
		label: 'Exchange',
		group: 'security',
		fields: [
			{ name: 'value', label: 'Exchange', type: 'text', def: '' }
		],
		summary: (p) => `exchange = ${(p.value ?? '?').toUpperCase()}`
	},
	{
		kind: 'sic_industry',
		label: 'SIC industry',
		group: 'security',
		fields: [
			{ name: 'code', label: 'SIC code', type: 'text', def: '', optional: true },
			{ name: 'prefix', label: 'Industry prefix', type: 'text', def: '', optional: true }
		],
		summary: (p) => (p.code ? `sic code = ${p.code}` : `sic ~ ${p.prefix ?? '?'}`)
	},
	// -- Fundamentals ------------------------------------------------------
	{
		kind: 'fundamentals_min',
		label: 'Fundamental ≥ min',
		group: 'fundamentals',
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
		summary: (p) => `fundamentals.${p.field ?? '?'} ≥ ${fmt(p.min ?? '?')}`
	},
	{
		kind: 'fundamentals_max',
		label: 'Fundamental ≤ max',
		group: 'fundamentals',
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
		summary: (p) => `fundamentals.${p.field ?? '?'} ≤ ${fmt(p.max ?? '?')}`
	},
	// -- Volume & liquidity (incl. the T219 market presets) ------------------
	{
		kind: 'avg_dollar_volume_min',
		label: 'Avg $ volume ≥ min',
		group: 'liquidity',
		fields: [{ name: 'min', label: 'Min $', type: 'number', def: '250000000' }],
		summary: (p) => `avg $vol ≥ ${fmt(p.min ?? '?')}`
	},
	{
		kind: 'min_price',
		label: 'Price ≥ min',
		group: 'liquidity',
		fields: [{ name: 'min', label: 'Min $', type: 'number', def: '20' }],
		summary: (p) => `price ≥ $${p.min ?? '?'}`
	},
	{
		kind: 'avg_volume_percentile_min',
		label: 'Volume percentile ≥ min',
		group: 'liquidity',
		fields: [{ name: 'min', label: 'Min pct', type: 'fraction', def: '0.5' }],
		summary: (p) => `volume pct ≥ ${p.min ?? '?'}`
	},
	{
		kind: 'listing_age_days_min',
		label: 'Listing age ≥ min days',
		group: 'liquidity',
		fields: [{ name: 'min_days', label: 'Min days', type: 'nonNegInt', def: '252' }],
		summary: (p) => `age ≥ ${p.min_days ?? '?'}d`
	},
	{
		kind: 'dollar_volume_percentile_min',
		label: '$ volume percentile ≥ min',
		group: 'liquidity',
		fields: [
			{ name: 'min', label: 'Min pct', type: 'fraction', def: '0.5' },
			{ name: 'window_days', label: 'Window days', type: 'positiveInt', def: '', optional: true }
		],
		summary: (p) => `$vol pct ≥ ${p.min ?? '?'}${p.window_days ? ` (${p.window_days}d)` : ''}`
	},
	// -- Technical -----------------------------------------------------------
	{
		kind: 'rsi_below',
		label: 'RSI below threshold',
		group: 'technical',
		fields: [
			{ name: 'period', label: 'Period', type: 'positiveInt', def: '14' },
			{ name: 'threshold', label: 'Threshold', type: 'percent', def: '30' }
		],
		summary: (p) => `RSI(${p.period ?? '?'}) ≤ ${p.threshold ?? '?'}`
	},
	{
		kind: 'rsi_above',
		label: 'RSI above threshold',
		group: 'technical',
		fields: [
			{ name: 'period', label: 'Period', type: 'positiveInt', def: '14' },
			{ name: 'threshold', label: 'Threshold', type: 'percent', def: '70' }
		],
		summary: (p) => `RSI(${p.period ?? '?'}) ≥ ${p.threshold ?? '?'}`
	},
	{
		kind: 'price_above_sma',
		label: 'Price ≥ SMA',
		group: 'technical',
		fields: [{ name: 'period', label: 'Period', type: 'positiveInt', def: '50' }],
		summary: (p) => `price ≥ SMA(${p.period ?? '?'})`
	},
	{
		kind: 'price_below_sma',
		label: 'Price ≤ SMA',
		group: 'technical',
		fields: [{ name: 'period', label: 'Period', type: 'positiveInt', def: '200' }],
		summary: (p) => `price ≤ SMA(${p.period ?? '?'})`
	},
	{
		kind: 'atr_percentile_min',
		label: 'ATR percentile ≥ min',
		group: 'technical',
		fields: [
			{ name: 'period', label: 'Period', type: 'positiveInt', def: '14' },
			{ name: 'min', label: 'Min pct', type: 'fraction', def: '0.5' }
		],
		summary: (p) => `ATR pct(${p.period ?? '?'}) ≥ ${p.min ?? '?'}`
	}
];

export const FILTER_GROUPS: { id: FilterGroup; label: string }[] = [
	{ id: 'security', label: 'Security' },
	{ id: 'fundamentals', label: 'Fundamentals' },
	{ id: 'liquidity', label: 'Volume & liquidity' },
	{ id: 'technical', label: 'Technical' }
];

/** Default row kind: a valid, universally-applicable liquidity preset. */
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
 * Client-side mirror of the declaration-time FilterError validation in
 * presets.py. Returns null when the row would emit an engine-valid filter.
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

/** Convert one valid row into an engine-native filter spec (null if invalid). */
export function rowToSpec(row: FilterRow): ScreenerFilterSpec | null {
	if (validateRow(row) !== null) return null;
	const spec = kindSpec(row.kind);
	const params: Record<string, string | number | boolean> = {};
	for (const field of spec?.fields ?? []) {
		const value = row.params[field.name];
		const raw = (value === undefined || value === null ? '' : String(value)).trim();
		if (!raw) continue;
		params[field.name] = field.type === 'text' || field.type === 'choice' ? raw : Number(raw);
	}
	return { kind: row.kind, params };
}

/** Valid filter specs for the current rows, in declaration order. */
export function buildFilters(rows: FilterRow[]): ScreenerFilterSpec[] {
	return rows
		.map(rowToSpec)
		.filter((spec): spec is ScreenerFilterSpec => spec !== null);
}

/**
 * Hydrate one saved-contract filter into an editable row. Unknown param
 * keys keep the catalog defaults (engine vocabulary drift must not break
 * the builder); unknown kinds carry their raw params and stay invalid
 * until re-edited.
 */
export function specToRow(spec: ScreenerFilterSpec, uid: number = nextRowUid()): FilterRow {
	const known = kindSpec(spec.kind);
	const params = defaultParams(spec.kind);
	if (known) {
		for (const field of known.fields) {
			const raw = spec.params?.[field.name];
			if (raw !== undefined && raw !== null && String(raw).trim() !== '') {
				params[field.name] = String(raw);
			}
		}
		return { uid, kind: spec.kind, params };
	}
	const carried: Record<string, string> = {};
	for (const [key, value] of Object.entries(spec.params ?? {})) carried[key] = String(value);
	return { uid, kind: spec.kind, params: carried };
}

function stringParams(params: Record<string, string | number | boolean> | undefined): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [key, value] of Object.entries(params ?? {})) out[key] = String(value);
	return out;
}

/** Human summary of one filter spec, for chips and saved-study lists. */
export function describeSpec(spec: ScreenerFilterSpec): string {
	const known = kindSpec(spec.kind);
	if (!known) {
		const entries = Object.entries(spec.params ?? {});
		return entries.length ? `${spec.kind}(${entries.map(([k, v]) => `${k}=${v}`).join(', ')})` : spec.kind;
	}
	return known.summary(stringParams(spec.params));
}

/**
 * Map a thrown API error to a readable message. The screening router
 * surfaces declaration-time validation failures (FilterError) as HTTP 422,
 * missing ids as 404, and unconfigured data ports as 503; api.ts prefixes
 * the status into its thrown messages.
 */
export function describeScreenerError(error: unknown): string {
	const message = error instanceof Error ? error.message : String(error);
	if (/unknown filter kind/i.test(message)) {
		return `The engine rejected a filter kind: ${message}`;
	}
	if (/\b422\b/.test(message)) {
		return 'The screening engine rejected this definition (HTTP 422, FilterError). Every filter kind and parameter is validated at declaration time — check the parameter names, types and ranges against the builder vocabulary.';
	}
	if (/\b404\b/.test(message)) {
		return 'That screener or stored result does not exist in this workspace (HTTP 404).';
	}
	if (/\b503\b/.test(message)) {
		return 'The screening data source is not configured for this workspace (HTTP 503).';
	}
	if (/Thesis API \d+/.test(message)) {
		return `The screener request failed server-side (${message}).`;
	}
	return message;
}
