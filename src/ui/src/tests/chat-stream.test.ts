import { describe, expect, it } from 'vitest';
import { chatStreamUrl, parseSseChunk, providerDeltaOf } from '$lib/sse';

describe('chat reply stream', () => {
	it('builds the chat-scoped stream URL', () => {
		expect(chatStreamUrl('http://api.test/', 'ws-1', 'chat-1')).toBe(
			'http://api.test/v1/workspaces/ws-1/chats/chat-1/events/stream'
		);
	});

	it('decodes provider delta frames', () => {
		const { frames } = parseSseChunk(
			'',
			'event: provider.delta\ndata: {"kind":"provider.delta","delta":{"messageId":"s1","text":"hi"}}\n\n'
		);
		expect(frames).toHaveLength(1);
		expect(providerDeltaOf(frames[0])?.text).toBe('hi');
	});

	it('ignores frames that are not provider deltas', () => {
		const { frames } = parseSseChunk('', 'event: chat.message\ndata: {"kind":"chat.message"}\n\n');
		expect(providerDeltaOf(frames[0])).toBeNull();
	});
});
