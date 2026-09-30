import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage, SubAgentNode } from '$lib/types';

const node: SubAgentNode = {
	subAgentId: 'node-1', parentSubAgentId: null, phase: 'creation', role: 'researcher',
	status: 'running', acceptsInput: true, workflowExecutionId: null,
	context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } }
};

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test', fixtureMode: false,
	listChatSubAgents: vi.fn(async () => [node]),
	listNodeEvents: vi.fn(async () => []),
	listExecutionEvents: vi.fn(async () => []),
	postNodeMessage: vi.fn()
}));

import ChatSurface from '$lib/components/chat/ChatSurface.svelte';

function message(id: string, role: ChatMessage['role'], scoped: boolean): ChatMessage {
	return {
		message_id: id, chat_id: 'chat-1', workspace_id: 'ws-1', actor_id: 'user',
		role, body: id, state: 'accepted', attachment_ids: [], created_at: '2026-01-01T00:00:00Z',
		updated_at: null, execution_id: null, intent_epoch: 0,
		kind: scoped ? 'sub_agent' : 'message', data: scoped ? { subAgentId: 'node-1' } : {}
	};
}

describe.each([false, true])('chat scrolling (sub-agent: %s)', (scoped) => {
	it('scrolls for a new user message only, not replies or tools', async () => {
		const props = {
			workspaceId: 'ws-1', chatId: 'chat-1', onSend: vi.fn(),
			initialScope: scoped ? 'conversation:chat-1/sub_agent:node-1' : null,
			messages: [message('initial', 'user', scoped)]
		};
		const view = render(ChatSurface, { props });
		const scroll = await screen.findByTestId(scoped ? 'scope-transcript-scroll' : 'transcript-scroll');
		await screen.findByText('initial');
		Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1000 });
		scroll.scrollTop = 100;
		const messages = [...props.messages, message('sent', 'user', scoped)];
		await view.rerender({ ...props, messages });
		await waitFor(() => expect(scroll.scrollTop).toBe(1000));
		scroll.scrollTop = 200;
		await view.rerender({ ...props, messages: [...messages, message('reply', 'assistant', scoped)] });
		expect(scroll.scrollTop).toBe(200);
		await view.rerender({ ...props, messages: [...messages, message('tool output', 'tool', scoped)] });
		expect(scroll.scrollTop).toBe(200);
		await view.rerender({
			...props, messages,
			streaming: { active: true, text: 'Streaming reply', reasoning: '' }
		});
		expect(scroll.scrollTop).toBe(200);
	});
});
