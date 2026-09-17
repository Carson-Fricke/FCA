import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fixture = vi.hoisted(() => ({
	subscribers: [] as Array<(item: Record<string, unknown>) => void>,
	anchor: {
		message_id: 'msg-wf',
		chat_id: 'chat-1',
		workspace_id: 'ws-1',
		actor_id: 'thesis-workflow',
		role: 'system',
		body: 'Hypothesis development',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-01-01T00:00:01Z',
		updated_at: null,
		execution_id: 'exec-1',
		intent_epoch: 0,
		kind: 'workflow',
		data: {
			currentPhase: 'ideation',
			executionId: 'exec-1',
			status: 'running',
			title: 'Hypothesis development',
			workflowId: 'hypothesis-development'
		}
	}
}));

function chatThread(chatId: string) {
	return {
		chat_id: chatId,
		workspace_id: 'ws-1',
		title: chatId,
		state: 'active',
		kind: 'chat' as const,
		owner_actor_id: 'user-alex',
		created_at: '2026-01-01T00:00:00Z',
		updated_at: null,
		investigation_ids: [],
		strategy_ids: []
	};
}

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	conversationTitleFrom: (body: string) => body,
	createChat: vi.fn(),
	deleteChat: vi.fn(),
	getExecution: vi.fn(async () => null),
	listChats: vi.fn(async () => [chatThread('chat-1'), chatThread('chat-2')]),
	listExecutionSubAgents: vi.fn(async () => []),
	listNodeEvents: vi.fn(async () => []),
	postMessage: vi.fn(),
	postNodeMessage: vi.fn()
}));

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeTimelineStream {
		async start(onItem: (item: Record<string, unknown>) => void) {
			fixture.subscribers.push(onItem);
		}
		stop(): void {}
	}
	class FakeExecutionEventStream {
		async start(): Promise<void> {}
		stop(): void {}
	}
	class FakeChatEventStream {
		async start(): Promise<void> {}
		stop(): void {}
	}
	return {
		...actual,
		TimelineStream: FakeTimelineStream,
		ExecutionEventStream: FakeExecutionEventStream,
		ChatEventStream: FakeChatEventStream
	};
});

import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import CopilotPanel from '$lib/components/shell/CopilotPanel.svelte';
import { appendMessage } from '$lib/chat-stream.svelte';
import type { ChatMessage } from '$lib/types';

const anchor = fixture.anchor as unknown as ChatMessage;

function timelineItem(seq: number) {
	return {
		id: `message:${fixture.anchor.message_id}`,
		chatId: 'chat-1',
		workspaceId: 'ws-1',
		kind: 'message',
		seq,
		ts: '2026-01-01T00:00:01Z',
		payload: fixture.anchor
	};
}

function timelineMessageItem(message: ChatMessage, seq: number) {
	return {
		id: `message:${message.message_id}`,
		chatId: message.chat_id,
		workspaceId: message.workspace_id,
		kind: 'message',
		seq,
		ts: message.created_at,
		payload: message
	};
}

beforeEach(() => {
	fixture.subscribers.length = 0;
});

describe('workflow anchor rendering', () => {
	it('renders a kind=workflow item as the WorkflowCard', async () => {
		render(ChatTranscript, { props: { messages: [anchor], workspaceId: 'ws-1' } });

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(screen.getByTestId('workflow-card-body')).toBeInTheDocument();
	});

	it('dedupes repeated anchor frames by message id', () => {
		expect(appendMessage([], anchor)).toHaveLength(1);
		expect(appendMessage(appendMessage([], anchor), anchor)).toHaveLength(1);
	});
});

describe('Copilot live timeline rendering', () => {
	it('renders a workflow card pushed over the unified timeline stream', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));

		fixture.subscribers.at(-1)?.(timelineItem(1));

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-body')).toBeInTheDocument();
	});

	it('clears the active timeline and shows a frontend-only draft on new conversation', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));

		const previous = {
			...anchor,
			message_id: 'old-message',
			kind: 'message' as const,
			role: 'assistant' as const,
			body: 'old conversation text'
		};
		fixture.subscribers.at(-1)?.(timelineMessageItem(previous, 1));
		expect(await screen.findByText('old conversation text')).toBeInTheDocument();

		await fireEvent.click(screen.getByTestId('copilot-new-conversation'));

		expect(screen.queryByText('old conversation text')).toBeNull();
		await fireEvent.click(screen.getByTestId('copilot-history'));
		expect(screen.getAllByTestId('copilot-history-item')[0]).toHaveTextContent(
			'New conversation'
		);
	});
});
