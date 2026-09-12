import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import StreamingReply from '$lib/components/chat/StreamingReply.svelte';
import ThinkingAccordion from '$lib/components/chat/ThinkingAccordion.svelte';
import {
	applyProviderDelta,
	type StreamingState
} from '$lib/chat-stream.svelte';
import type { ChatMessage } from '$lib/types';

const EMPTY: StreamingState = { id: null, text: '', reasoning: '' };

describe('reasoning delta accumulation', () => {
	it('routes kind: "reasoning" chunks into reasoning, never the answer', () => {
		let state = applyProviderDelta(EMPTY, { messageId: 's1', start: true });
		expect(state.id).toBe('s1');

		state = applyProviderDelta(state, { kind: 'reasoning', text: 'weighing ' });
		state = applyProviderDelta(state, { kind: 'reasoning', text: 'the options' });
		state = applyProviderDelta(state, { text: 'Answer ' });
		state = applyProviderDelta(state, { text: 'text' });

		expect(state.reasoning).toBe('weighing the options');
		expect(state.text).toBe('Answer text');
	});

	it('resets both buffers on a start frame and clears on done', () => {
		let state: StreamingState = { id: 'old', text: 'stale', reasoning: 'old thoughts' };
		state = applyProviderDelta(state, { messageId: 's2', start: true });
		expect(state).toEqual({ id: 's2', text: '', reasoning: '' });

		state = applyProviderDelta(state, { kind: 'reasoning', text: 'thought' });
		state = applyProviderDelta(state, { text: 'answer' });
		state = applyProviderDelta(state, { done: true });
		expect(state).toEqual({ id: null, text: '', reasoning: '' });
	});
});

describe('ThinkingAccordion', () => {
	it('is collapsed by default and toggles on click', async () => {
		render(ThinkingAccordion, { props: { text: 'internal reasoning' } });
		await tick();

		const toggle = screen.getByTestId('thinking-accordion-toggle');
		expect(toggle).toHaveAttribute('aria-expanded', 'false');
		expect(screen.queryByTestId('thinking-accordion-body')).toBeNull();

		await fireEvent.click(toggle);
		await tick();
		expect(toggle).toHaveAttribute('aria-expanded', 'true');
		expect(screen.getByTestId('thinking-accordion-body')).toHaveTextContent('internal reasoning');

		await fireEvent.click(toggle);
		await tick();
		expect(toggle).toHaveAttribute('aria-expanded', 'false');
		expect(screen.queryByTestId('thinking-accordion-body')).toBeNull();
	});
});

describe('StreamingReply reasoning', () => {
	it('renders the thinking accordion above the answer while reasoning streams', async () => {
		const { container } = render(StreamingReply, {
			props: { active: true, text: 'final answer', reasoning: 'live thoughts' }
		});
		await tick();

		const accordion = screen.getByTestId('streaming-reply-thinking');
		expect(accordion).toBeInTheDocument();
		expect(accordion.querySelector('[aria-expanded="false"]')).not.toBeNull();
		// The answer text is still rendered; reasoning stays hidden until expanded.
		expect(container.textContent).toContain('final answer');
		expect(container.textContent).not.toContain('live thoughts');
	});

	it('hides the accordion when there is no reasoning', async () => {
		render(StreamingReply, { props: { active: true, text: 'answer only' } });
		await tick();
		expect(screen.queryByTestId('streaming-reply-thinking')).toBeNull();
	});
});

function message(overrides: Partial<ChatMessage>): ChatMessage {
	return {
		message_id: 'msg-1',
		chat_id: 'chat-1',
		workspace_id: 'ws-1',
		actor_id: 'user-alex',
		role: 'assistant',
		body: 'hello',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-09-11T00:00:00Z',
		updated_at: null,
		execution_id: null,
		intent_epoch: 0,
		...overrides
	};
}

describe('ChatTranscript persisted reasoning', () => {
	it('renders the accordion for persisted messages that include reasoning', async () => {
		const withReasoning = message({ message_id: 'msg-r', reasoning: 'stored thoughts' });
		const withoutReasoning = message({ message_id: 'msg-n', body: 'plain reply' });
		render(ChatTranscript, {
			props: {
				messages: [withReasoning, withoutReasoning],
				workspaceId: 'w1',
				onConfirmConsent: undefined,
				onRejectConsent: undefined
			}
		});
		await tick();

		const accordion = screen.getByTestId('thinking-msg-r');
		expect(accordion).toBeInTheDocument();
		expect(screen.queryByTestId('thinking-msg-n')).toBeNull();
		expect(accordion.querySelector('[aria-expanded="false"]')).not.toBeNull();
	});
});
