import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import type { ChatMessage, ToolResult } from '$lib/types';

function message(overrides: Partial<ChatMessage>): ChatMessage {
	return {
		message_id: 'm0',
		chat_id: 'c1',
		workspace_id: 'w1',
		actor_id: 'operator',
		role: 'user',
		body: '',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-01-01T00:00:00Z',
		updated_at: null,
		execution_id: null,
		intent_epoch: 0,
		...overrides
	};
}

const toolResult: ToolResult = {
	tool: 'run_backtest',
	callId: 'call-1',
	ok: true,
	summary: 'run_backtest completed',
	structured: { boundary: 'local', exit_code: 0 },
	text: 'ok',
	failure: null
};

describe('ChatTranscript tool-result placement', () => {
	it('renders a tool message inline at its position in the chain', async () => {
		const messages: ChatMessage[] = [
			message({ message_id: 'm1', role: 'user', body: 'run it' }),
			message({
				message_id: 'm2',
				role: 'tool',
				actor_id: 'thesis-agent',
				body: 'run_backtest completed',
				tool_result: toolResult
			}),
			message({
				message_id: 'm3',
				role: 'assistant',
				actor_id: 'thesis-agent',
				body: 'Done.'
			})
		];

		const { container } = render(ChatTranscript, {
			props: {
				messages,
				workspaceId: 'w1',
				onConfirmConsent: vi.fn(),
				onRejectConsent: vi.fn()
			}
		});
		await tick();

		const roles = Array.from(container.querySelectorAll('[data-role]')).map((el) =>
			el.getAttribute('data-role')
		);
		expect(roles).toEqual(['user', 'tool', 'assistant']);

		const card = screen.getByTestId('tool-result-card');
		expect(card).toHaveAttribute('data-tool', 'run_backtest');
	});
});
