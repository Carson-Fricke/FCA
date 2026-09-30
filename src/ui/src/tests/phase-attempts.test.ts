import { describe, expect, it } from 'vitest';
import { derivePhaseAttempts } from '$lib/subagent';
import type { ExecutionEvent, ExecutionEventType } from '$lib/types';

const PHASE = 'falsification_trials';

function event(
	cursor: number,
	eventType: ExecutionEventType,
	payload: Record<string, unknown>
): ExecutionEvent {
	return {
		actorId: 'thesis-workflow',
		actorScope: 'workflow',
		correlationId: 'exec-1',
		cursor,
		eventId: `evt-${cursor}`,
		eventType,
		executionId: 'exec-1',
		intentEpoch: 0,
		lateResult: false,
		occurredAt: `2026-01-01T00:00:${String(cursor).padStart(2, '0')}Z`,
		payload,
		workspaceId: 'ws-1'
	};
}

describe('derivePhaseAttempts', () => {
	it('groups a phase re-entry into separate attempts', () => {
		const runs = derivePhaseAttempts(
			[
				event(3, 'workflow.phase_result', {
					phase: PHASE,
					summary: 'first result',
					artifact: { type: 'note' }
				}),
				event(1, 'workflow.phase', { phase: PHASE, reason: 'entered' }),
				event(2, 'workflow.progress', { phase: PHASE, text: 'first progress' }),
				event(4, 'workflow.phase', { phase: 'verdict' }),
				event(5, 'workflow.phase', { phase: PHASE, reason: 'loopback' }),
				event(6, 'workflow.progress', { phase: PHASE, text: 'second progress' }),
				event(7, 'workflow.phase_result', {
					phase: PHASE,
					summary: 'second result',
					artifact: { type: 'note' }
				})
			],
			PHASE
		);

		expect(runs).toHaveLength(2);
		expect(runs[0]).toMatchObject({
			attempt: 1,
			progress: [{ text: 'first progress', at: '2026-01-01T00:00:02Z' }],
			result: { summary: 'first result', artifact: { type: 'note' } },
			error: null
		});
		expect(runs[1]).toMatchObject({
			attempt: 2,
			progress: [{ text: 'second progress', at: '2026-01-01T00:00:06Z' }],
			result: { summary: 'second result', artifact: { type: 'note' } },
			error: null
		});
	});

	it('keeps the error variant for a failed attempt', () => {
		const runs = derivePhaseAttempts(
			[
				event(1, 'workflow.phase', { phase: PHASE }),
				event(2, 'workflow.phase_result', {
					phase: PHASE,
					summary: 'trials failed',
					artifact: { type: 'error', message: 'harness exploded' }
				})
			],
			PHASE
		);

		expect(runs[0].result).toBeNull();
		expect(runs[0].error).toEqual({ message: 'harness exploded' });
	});

	it('treats output without an entry event as the first attempt', () => {
		const runs = derivePhaseAttempts(
			[event(1, 'workflow.progress', { phase: PHASE, text: 'orphan progress' })],
			PHASE
		);

		expect(runs).toHaveLength(1);
		expect(runs[0].attempt).toBe(1);
		expect(runs[0].progress).toEqual([
			{ text: 'orphan progress', at: '2026-01-01T00:00:01Z' }
		]);
	});

	it('ignores events belonging to another phase', () => {
		const runs = derivePhaseAttempts(
			[
				event(1, 'workflow.phase', { phase: 'verdict' }),
				event(2, 'workflow.progress', { phase: 'verdict', text: 'verdict progress' })
			],
			PHASE
		);

		expect(runs).toEqual([]);
	});
});
