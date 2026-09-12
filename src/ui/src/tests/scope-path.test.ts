import { describe, expect, it } from 'vitest';
import {
	applyHashTarget,
	parseScope,
	ScopeController,
	scopeFromHash,
	serializeScope,
	type ScopeSegment
} from '$lib/scope.svelte';

describe('scope path grammar', () => {
	it('round-trips segments through the query value', () => {
		const segments: ScopeSegment[] = [
			{ kind: 'conversation', id: 'chat-1', label: 'Conversation' },
			{ kind: 'workflow', id: 'exec-1', label: 'Workflow' },
			{ kind: 'sub_agent', id: 'sub-1', label: 'Researcher' }
		];
		expect(serializeScope(segments)).toBe('conversation:chat-1/workflow:exec-1/sub_agent:sub-1');
		const parsed = parseScope(serializeScope(segments));
		expect(parsed.map((segment) => [segment.kind, segment.id])).toEqual([
			['conversation', 'chat-1'],
			['workflow', 'exec-1'],
			['sub_agent', 'sub-1']
		]);
	});

	it('drops malformed entries', () => {
		expect(parseScope('not-a-kind:x//conversation:chat-1')).toHaveLength(1);
	});

	it('deep-links a sub-agent from the hash', () => {
		const base = parseScope('conversation:chat-1/workflow:exec-1');
		const target = scopeFromHash('#sub_agent=sub-2');
		expect(target?.id).toBe('sub-2');
		const merged = applyHashTarget(base, target);
		expect(merged.map((segment) => segment.id)).toEqual(['chat-1', 'exec-1', 'sub-2']);
		// An existing target re-selects instead of duplicating.
		expect(applyHashTarget(merged, target)).toHaveLength(3);
	});
});

describe('ScopeController', () => {
	it('enters, refreshes without duplicating, and goes back', () => {
		const controller = new ScopeController('thesis.scope.unit');
		controller.reset([{ kind: 'conversation', id: 'chat-1', label: 'Conversation' }]);
		controller.push({ kind: 'workflow', id: 'exec-1', label: 'Workflow' });
		controller.push({ kind: 'sub_agent', id: 'sub-1', label: 'Researcher' });
		expect(controller.segments.map((segment) => segment.id)).toEqual(['chat-1', 'exec-1', 'sub-1']);

		// Re-entering the same node truncates to it and refreshes its label.
		controller.push({ kind: 'sub_agent', id: 'sub-1', label: 'Researcher v2' });
		expect(controller.segments.map((segment) => segment.id)).toEqual(['chat-1', 'exec-1', 'sub-1']);
		expect(controller.active?.label).toBe('Researcher v2');

		controller.back();
		expect(controller.active?.id).toBe('exec-1');
		controller.back();
		expect(controller.active?.id).toBe('chat-1');
		expect(controller.canGoBack).toBe(false);
	});

	it('navigates to a breadcrumb index', () => {
		const controller = new ScopeController('thesis.scope.unit2');
		controller.reset([{ kind: 'conversation', id: 'chat-1', label: 'Conversation' }]);
		controller.push({ kind: 'workflow', id: 'exec-1', label: 'Workflow' });
		controller.push({ kind: 'sub_agent', id: 'sub-1', label: 'Researcher' });
		controller.navigateTo(0);
		expect(controller.segments).toHaveLength(1);
		expect(controller.active?.kind).toBe('conversation');
	});
});
