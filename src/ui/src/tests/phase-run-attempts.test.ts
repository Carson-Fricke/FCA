import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { ExecutionEvent, ExecutionEventType } from '$lib/types';
import { timelineFixture } from './timeline-fixture';
import { phaseScopeId, serializeScope } from '$lib/scope.svelte';

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	listChatSubAgents: vi.fn(async () => []),
	postNodeMessage: vi.fn(),
	getWorkflowDefinition: vi.fn(async () => { throw new Error('Unavailable'); })
}));


import ChatSurface from '$lib/components/chat/ChatSurface.svelte';

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

const initialScope = serializeScope([{ kind: 'conversation', id: 'chat-1', label: 'Conversation' },
	{ kind: 'phase', id: phaseScopeId('exec-1', PHASE), label: PHASE }]);

const events: ExecutionEvent[] = [
	event(1, 'workflow.phase', { phase: PHASE, reason: 'entered' }),
	event(2, 'workflow.progress', { phase: PHASE, text: 'first progress' }),
	event(3, 'workflow.phase_result', {
		phase: PHASE,
		summary: 'first result',
		artifact: { type: 'note' }
	}),
	event(4, 'workflow.phase', { phase: 'verdict', reason: 'verdict requested' }),
	event(5, 'workflow.phase', { phase: PHASE, reason: 'loopback' }),
	event(6, 'workflow.progress', { phase: PHASE, text: 'second progress' }),
	event(7, 'workflow.phase_result', {
		phase: PHASE,
		summary: 'second result',
		artifact: { type: 'note' }
	})
];

describe('phase attempt selection', () => {
	it('lists every attempt and shows the selected attempt output', async () => {
		render(ChatSurface, {
			props: {
				workspaceId: 'ws-1',
				chatId: 'chat-1',
				timeline: timelineFixture([], events),
				onSend: vi.fn(),
				initialScope
			}
		});

		const select = await screen.findByTestId('phase-attempt-select');
		expect(select).toHaveValue('2');
		expect(screen.getByTestId('phase-run-result')).toHaveTextContent('second result');
		expect(screen.getByTestId('phase-run-log')).toHaveTextContent('second progress');

		await fireEvent.change(select, { target: { value: '1' } });

		expect(select).toHaveValue('1');
		expect(screen.getByTestId('phase-run-result')).toHaveTextContent('first result');
		expect(screen.getByTestId('phase-run-log')).toHaveTextContent('first progress');
		expect(screen.queryByText('second progress')).toBeNull();
	});

	it('omits the selector while the phase has a single attempt', async () => {
		render(ChatSurface, {
			props: {
				workspaceId: 'ws-1',
				chatId: 'chat-1',
				timeline: timelineFixture([], events.slice(0, 3)),
				onSend: vi.fn(),
				initialScope
			}
		});

		await screen.findByTestId('phase-run-view');
		expect(screen.queryByTestId('phase-attempt-select')).toBeNull();
		expect(screen.getByTestId('phase-run-result')).toHaveTextContent('first result');
	});
});
