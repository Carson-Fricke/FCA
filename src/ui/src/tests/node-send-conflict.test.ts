import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { WorkflowExecutionRecord } from '$lib/types';

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return {
		...actual,
		apiBaseUrl: 'http://api.test',
		fixtureMode: false,
		listChatSubAgents: vi.fn(async () => [
			{
				subAgentId: 'subagent-a',
				parentSubAgentId: 'subagent-root',
				phase: 'hypothesis_creation',
				role: 'Thesis hypothesis-creation orchestrator',
				status: 'running',
				acceptsInput: true,
				context: {
					isolated: true,
					knowledge_cutoff: null,
					mcp: { enabled: true, tools: ['validate_study'] }
				},
				workflowExecutionId: 'exec-1'
			}
		]),
		listExecutionEvents: vi.fn(async () => []),
		listNodeEvents: vi.fn(async () => []),
		postNodeMessage: vi.fn(async () => {
			throw new actual.ThesisApiError(409, '/messages');
		})
	};
});

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeExecutionEventStream {
		async start(): Promise<void> {}
		stop(): void {}
	}
	return { ...actual, ExecutionEventStream: FakeExecutionEventStream };
});

import ChatSurface from '$lib/components/chat/ChatSurface.svelte';

const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1',
	chat_id: 'chat-1',
	trigger_message_id: 'msg-1',
	workflow_id: 'hypothesis-development',
	workflow_version: 'v1',
	requested_by: 'user-alex',
	state: 'running',
	phase: 'hypothesis_creation',
	intent_epoch: 0,
	steering_message_ids: [],
	created_at: '2026-01-01T00:00:00Z'
};

describe('node input rejection', () => {
	it('surfaces a 409 and keeps the operator draft', async () => {
		render(ChatSurface, {
			props: {
				workspaceId: 'ws-1',
				chatId: 'chat-1',
				messages: [],
				events: [],
				execution,
				onSend: vi.fn(),
				initialScope: 'conversation:chat-1/sub_agent:subagent-a'
			}
		});

		const input = await screen.findByTestId('sub-agent-composer-input');
		await fireEvent.input(input, { target: { value: 'please revise the model' } });
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));

		const banner = await screen.findByTestId('node-send-error');
		expect(banner).toHaveTextContent('no longer accepts input');
		expect((input as HTMLTextAreaElement).value).toBe('please revise the model');
	});
});
