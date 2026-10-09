/**
 * Deterministic Rust source generation for screening programs.
 *
 * The generator assembles one `ScreeningProgram` from catalog rows using only
 * the frozen authoring SDK surface (`ctx.universe()`, `ctx.bars()`,
 * `ctx.facts()`); the harness JIT compiler is authoritative and its
 * diagnostics are surfaced by the run response, so the only client-side
 * pre-flight is a light scan for unbalanced brackets and the two required
 * authoring pieces.
 *
 * A single machine-readable marker comment carries the builder rows inside
 * the source, so a saved definition hydrates the filter builder without a
 * second persistence channel: the Thesis contract stores authored source,
 * never a filter document.
 */

import {
	describeRow,
	emitFragment,
	kindSpec,
	nextRowUid,
	validateRow
} from './filterCatalog';
import type { FilterRow } from './filterCatalog';

export const SCREENER_STRUCT_NAME = 'UiScreener';

/** Machine-readable builder-row marker; carries the row JSON on one line. */
export const ROWS_MARKER = '// fca-screener-rows: ';

export interface ScriptIssue {
	line: number | null;
	message: string;
	severity: 'error' | 'warning';
}

const BASE_INDENT = '            ';

function indent(text: string, spaces: string): string {
	return text
		.split('\n')
		.map((line) => (line ? `${spaces}${line}` : line))
		.join('\n');
}

function rowMarker(rows: FilterRow[]): string {
	return (
		ROWS_MARKER +
		JSON.stringify(rows.map((row) => ({ kind: row.kind, params: row.params })))
	);
}

/**
 * Generate one deterministic `ScreeningProgram` from catalog rows.
 *
 * Invalid rows emit nothing (the builder shows their error); valid rows emit
 * their fragment in declaration order with a comment naming the condition.
 * The emitted program always records the last close when a symbol has bars,
 * so `close` is available as a rank metric for any filter set.
 */
export function generateScreenerSource(rows: FilterRow[], interval: string = '1d'): string {
	const validRows = rows.filter((row) => validateRow(row) === null);
	const usesFacts = validRows.some((row) => kindSpec(row.kind)?.surface === 'facts');
	const fragments = validRows
		.map((row) => {
			const fragment = emitFragment(row);
			if (fragment === null) return null;
			return `${BASE_INDENT}// filter: ${describeRow(row)}\n${indent(fragment, BASE_INDENT)}`;
		})
		.filter((fragment): fragment is string => fragment !== null);
	const factsLine = usesFacts ? `\n${BASE_INDENT}let facts = ctx.facts(symbol);` : '';
	const filterBlock = fragments.length ? `\n${fragments.join('\n')}\n` : '';

	return `use std::collections::BTreeMap;

use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_screening_program, ScreenContext, ScreenRow, ScreeningProgram,
};

${rowMarker(rows)}

struct ${SCREENER_STRUCT_NAME};

impl ScreeningProgram for ${SCREENER_STRUCT_NAME} {
    fn new(_parameters: Value) -> Self {
        Self
    }

    fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow> {
        let mut rows = Vec::new();
        for symbol in ctx.universe().iter() {
            let bars = ctx.bars(symbol, ${JSON.stringify(interval)});${factsLine}
            let mut metrics = BTreeMap::new();
            if let Some(bar) = bars.last() {
                metrics.insert("close".to_string(), bar.close.clone());
            }${filterBlock}
            rows.push(ScreenRow {
                symbol: symbol.clone(),
                metrics,
            });
        }
        rows
    }
}

export_screening_program!(${SCREENER_STRUCT_NAME});
`;
}

/** The no-filter template: a valid program that records every symbol's close. */
export const SCREENER_TEMPLATE = generateScreenerSource([]);

/**
 * Hydrate builder rows from a generated source's marker comment. Returns
 * null when the source carries no marker or the marker is not a row list;
 * callers treat that as hand-authored source and leave the builder empty.
 */
export function parseFilterRows(source: string): FilterRow[] | null {
	const line = source
		.replace(/\r\n/g, '\n')
		.split('\n')
		.find((candidate) => candidate.trimStart().startsWith(ROWS_MARKER));
	if (!line) return null;
	const trimmed = line.trimStart();
	let parsed: unknown;
	try {
		parsed = JSON.parse(trimmed.slice(ROWS_MARKER.length));
	} catch {
		return null;
	}
	if (!Array.isArray(parsed)) return null;
	const rows: FilterRow[] = [];
	for (const entry of parsed) {
		if (typeof entry !== 'object' || entry === null) return null;
		const record = entry as { kind?: unknown; params?: unknown };
		if (typeof record.kind !== 'string' || !record.kind) return null;
		const params: Record<string, string> = {};
		if (record.params !== undefined) {
			if (typeof record.params !== 'object' || record.params === null || Array.isArray(record.params)) {
				return null;
			}
			for (const [key, value] of Object.entries(record.params as Record<string, unknown>)) {
				if (typeof value !== 'string' && typeof value !== 'number') return null;
				params[key] = String(value);
			}
		}
		rows.push({ uid: nextRowUid(), kind: record.kind, params });
	}
	return rows;
}

function matches(open: string, close: string): boolean {
	return (
		(open === '(' && close === ')') ||
		(open === '[' && close === ']') ||
		(open === '{' && close === '}')
	);
}

/**
 * Light pre-flight: non-empty source, balanced brackets (comments and string
 * literals skipped), one `fn screen(` definition, and the
 * `export_screening_program!` entry macro. This is not a parser; the harness
 * compiler remains the authority and its errors surface on the run.
 */
export function lintScreenerSource(source: string): ScriptIssue[] {
	const text = source.replace(/\r\n/g, '\n');
	if (!text.trim()) {
		return [
			{ line: null, message: 'screener source must be a non-empty string', severity: 'error' }
		];
	}

	const issues: ScriptIssue[] = [];
	const stack: { ch: string; line: number }[] = [];
	let mode: 'code' | 'line-comment' | 'block-comment' | 'string' | 'char' = 'code';
	let line = 1;
	for (let index = 0; index < text.length; index += 1) {
		const ch = text[index];
		const next = text[index + 1];
		if (ch === '\n') {
			line += 1;
			if (mode === 'line-comment') mode = 'code';
			continue;
		}
		if (mode === 'line-comment') continue;
		if (mode === 'block-comment') {
			if (ch === '*' && next === '/') {
				mode = 'code';
				index += 1;
			}
			continue;
		}
		if (mode === 'string' || mode === 'char') {
			if (ch === '\\') {
				index += 1;
				continue;
			}
			if ((mode === 'string' && ch === '"') || (mode === 'char' && ch === "'")) mode = 'code';
			continue;
		}
		if (ch === '/' && next === '/') {
			mode = 'line-comment';
			index += 1;
			continue;
		}
		if (ch === '/' && next === '*') {
			mode = 'block-comment';
			index += 1;
			continue;
		}
		if (ch === '"') {
			mode = 'string';
			continue;
		}
		if (ch === "'") {
			// Only a complete char literal switches modes; lifetimes stay code.
			const closed =
				text[index + 1] === '\\' ? text[index + 3] === "'" : text[index + 2] === "'";
			if (closed) mode = 'char';
			continue;
		}
		if (ch === '(' || ch === '[' || ch === '{') {
			stack.push({ ch, line });
		} else if (ch === ')' || ch === ']' || ch === '}') {
			const open = stack.pop();
			if (!open || !matches(open.ch, ch)) {
				issues.push({ line, message: `unbalanced '${ch}'`, severity: 'error' });
				stack.length = 0;
			}
		}
	}
	if (stack.length) {
		const open = stack[stack.length - 1];
		issues.push({
			line: open.line,
			message: `unbalanced '${open.ch}' — check bracket pairing from this line`,
			severity: 'error'
		});
	}

	if (!/\bfn\s+screen\s*\(/.test(text)) {
		issues.push({
			line: null,
			message: 'the program must define `fn screen(&mut self, ctx: &ScreenContext)`',
			severity: 'error'
		});
	}
	if (!/export_screening_program!\s*\(/.test(text)) {
		issues.push({
			line: null,
			message: 'the source must export the program with `export_screening_program!(...)`',
			severity: 'error'
		});
	}

	return issues;
}
