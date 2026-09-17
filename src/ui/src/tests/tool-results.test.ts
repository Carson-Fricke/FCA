import { describe, expect, it } from 'vitest';
import {
	isRecord,
	readArray,
	readNumber,
	readRecord,
	readString
} from '$lib/components/chat/tool-results/safe';
import {
	toolResultComponent,
	TOOL_RESULT_RENDERERS
} from '$lib/components/chat/tool-results/registry';
import GenericToolCard from '$lib/components/chat/tool-results/GenericToolCard.svelte';
import BacktestToolCard from '$lib/components/chat/tool-results/BacktestToolCard.svelte';

describe('toolResultComponent', () => {
	it('maps run_backtest to the backtest card', () => {
		expect(toolResultComponent('run_backtest')).toBe(BacktestToolCard);
	});

	it('falls back to the generic card for unknown tools', () => {
		expect(toolResultComponent('not_a_real_tool')).toBe(GenericToolCard);
	});

	it('registers a renderer for every custom tool', () => {
		expect(Object.keys(TOOL_RESULT_RENDERERS).length).toBeGreaterThan(0);
		expect(toolResultComponent('get_run')).not.toBeNull();
	});
});

describe('toolResultComponent datum-type resolution', () => {
	it('resolves a well-known datum type to the generic card today', () => {
		expect(TOOL_RESULT_RENDERERS['workflow_execution']).toBe(GenericToolCard);
		expect(toolResultComponent('some_harness_tool', 'workflow_execution')).toBe(GenericToolCard);
	});

	it('keeps the harness tool-name fallback when no datum type is set', () => {
		expect(toolResultComponent('run_backtest')).toBe(BacktestToolCard);
		expect(toolResultComponent('run_backtest', null)).toBe(BacktestToolCard);
	});

	it('resolves a datum type that equals a tool name via the datum type path', () => {
		expect(toolResultComponent('not_a_real_tool', 'run_backtest')).toBe(BacktestToolCard);
		expect(toolResultComponent('run_backtest', 'run_backtest')).toBe(BacktestToolCard);
	});

	it('prefers the datum type over a known tool name', () => {
		expect(toolResultComponent('list_artifacts', 'run_backtest')).toBe(BacktestToolCard);
	});

	it('falls back to the generic card for an unknown datum type and unknown tool', () => {
		expect(toolResultComponent('not_a_real_tool', 'not_a_real_datum_type')).toBe(GenericToolCard);
	});
});

describe('safe record helpers', () => {
	const source: Record<string, unknown> = {
		name: 'run-1',
		count: 3,
		items: [1, 2],
		nested: { a: 1 },
		missing: null
	};

	it('reads strings, numbers, arrays, and records defensively', () => {
		expect(readString(source, 'name')).toBe('run-1');
		expect(readString(source, 'missing')).toBeNull();
		expect(readString(source, 'count')).toBeNull();
		expect(readNumber(source, 'count')).toBe(3);
		expect(readNumber(source, 'name')).toBeNull();
		expect(readArray<number>(source, 'items')).toEqual([1, 2]);
		expect(readArray(source, 'name')).toEqual([]);
		expect(readRecord(source, 'nested')).toEqual({ a: 1 });
		expect(readRecord(source, 'name')).toEqual({});
	});

	it('recognizes plain records but not arrays', () => {
		expect(isRecord({ a: 1 })).toBe(true);
		expect(isRecord([1])).toBe(false);
		expect(isRecord(null)).toBe(false);
	});
});
