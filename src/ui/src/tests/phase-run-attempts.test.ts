import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { ExecutionEvent, ExecutionEventType, WorkflowExecutionRecord } from '$lib/types';

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	listChatSubAgents: vi.fn(async () => []),
	listExecutionEvents: vi.fn(async () => []),
	listNodeEvents: vi.fn(async () => []),
	postNodeMessage: vi.fn()
}));

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeExecutionEventStream {
		async start(): Promise<void> {}
		stop(): void {}
	}
	return { ...actual, ExecutionEventStream: FakeExecutionEventStream };
});

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

const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1',
	chat_id: 'chat-1',
	trigger_message_id: 'msg-1',
	workflow_id: 'hypothesis-development',
	workflow_version: 'v1',
	requested_by: 'user-alex',
	state: 'running',
	phase: PHASE,
	intent_epoch: 0,
	steering_message_ids: [],
	created_at: '2026-01-01T00:00:00Z'
};

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
				messages: [],
				events,
				execution,
				onSend: vi.fn(),
				initialScope: `conversation:chat-1/phase:${PHASE}`
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
				messages: [],
				events: events.slice(0, 3),
				execution,
				onSend: vi.fn(),
				initialScope: `conversation:chat-1/phase:${PHASE}`
			}
		});

		await screen.findByTestId('phase-run-view');
		expect(screen.queryByTestId('phase-attempt-select')).toBeNull();
		expect(screen.getByTestId('phase-run-result')).toHaveTextContent('first result');
	});
});
