import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fixture = vi.hoisted(() => ({
	subscribers: [] as Array<(frame: { id: string | null; event: string; data: Record<string, unknown> }) => void>,
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
	listChats: vi.fn(async () => [chatThread('chat-1'), chatThread('chat-2')]),
	listMessages: vi.fn(async (_workspaceId: string, chatId: string) =>
		chatId === 'chat-2' ? [fixture.anchor] : []
	),
	postMessage: vi.fn()
}));

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeChatEventStream {
		async start(onFrame: (frame: { id: string | null; event: string; data: Record<string, unknown> }) => void) {
			fixture.subscribers.push(onFrame);
		}
		stop(): void {}
	}
	return { ...actual, ChatEventStream: FakeChatEventStream };
});

import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import CopilotPanel from '$lib/components/shell/CopilotPanel.svelte';
import { appendMessage } from '$lib/chat-stream.svelte';
import type { ChatMessage } from '$lib/types';

const anchor = fixture.anchor as unknown as ChatMessage;

beforeEach(() => {
	fixture.subscribers.length = 0;
});

describe('workflow anchor rendering', () => {
	it('renders a kind=workflow item as the WorkflowCard with enter/link affordances', async () => {
		render(ChatTranscript, { props: { messages: [anchor], workspaceId: 'ws-1' } });

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-open-full')).toHaveAttribute(
			'href',
			'/w/ws-1/workflows/exec-1'
		);
		expect(screen.getByTestId('workflow-card-status')).toHaveTextContent('running');
	});

	it('dedupes repeated anchor frames by message id', () => {
		expect(appendMessage([], anchor)).toHaveLength(1);
		expect(appendMessage(appendMessage([], anchor), anchor)).toHaveLength(1);
	});
});

describe('Copilot live workflow rendering', () => {
	it('appends a live chat.message workflow frame and renders the card', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));

		const onFrame = fixture.subscribers.at(-1);
		onFrame?.({ id: null, event: 'chat.message', data: { message: fixture.anchor } });

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-body')).toBeInTheDocument();
	});

	it('clears the streaming indicator when done arrives', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));
		const onFrame = fixture.subscribers.at(-1);

		onFrame?.({ id: null, event: 'provider.delta', data: { delta: { messageId: 's1', start: true } } });
		expect(await screen.findByTestId('copilot-streaming-reply')).toBeInTheDocument();

		onFrame?.({ id: null, event: 'provider.delta', data: { delta: { messageId: 's1', text: 'hi' } } });
		await tick();
		onFrame?.({ id: null, event: 'provider.delta', data: { delta: { messageId: 's1', done: true } } });

		await waitFor(() =>
			expect(screen.queryByTestId('copilot-streaming-reply')).toBeNull()
		);
	});

	it('retains the card when navigating away and back re-fetches the transcript', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await screen.findByTestId('copilot-history');

		await fireEvent.click(screen.getByTestId('copilot-history'));
		(await screen.findAllByTestId('copilot-history-item'))[1].click();
		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();

		await fireEvent.click(screen.getByTestId('copilot-history'));
		(await screen.findAllByTestId('copilot-history-item'))[0].click();
		await waitFor(() => expect(screen.queryByTestId('workflow-card')).toBeNull());

		await fireEvent.click(screen.getByTestId('copilot-history'));
		(await screen.findAllByTestId('copilot-history-item'))[1].click();
		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
	});
});
