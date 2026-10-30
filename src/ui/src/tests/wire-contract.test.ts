import { toTranscriptMessage } from '$lib/transcript';
/// <reference types="node" />
import { readFile } from 'node:fs/promises';
import { describe, expect, it } from 'vitest';
import { generateWire } from '../../generate-wire.mjs';
import { decodeWire, WireDecodeError } from '$lib/wire';
import { createChat, deleteChat, listChats, listTimelinePage, postMessage, postNodeMessage } from '$lib/api';
import { TimelineStream } from '$lib/sse';
import backendMessageFixture from './backend-message.json';
import backendTree from './backend-tree.json';
import backendThread from './backend-thread.json';
import { aggregateDatums, aggregateMessage } from './timeline-fixture';
import { toChatMessage } from '$lib/transcript';
const backendMessage = decodeWire('ChatMessageWire', backendMessageFixture, 'test/backend-message');

describe('committed backend wire contract', () => {
    it('accepts null metadata and preserves a neutral delivery without a tool request identity', () => {
        const plain = decodeWire('ChatMessageWire', { ...backendMessage, data: null }, 'plain-message');
        expect(plain.data).toBeNull();
        const delivered = { datumType: 'agent_prompt', ok: true, summary: 'Seed', text: 'Seed', structured: {}, failure: null };
        const message = decodeWire('ChatMessageWire', { ...backendMessage, tool_result: delivered }, 'neutral-delivery');
        const projected = toChatMessage(message);
        expect(projected.tool_result).toEqual({ ...delivered, tool: null, callId: null });
        expect(message.tool_result).toEqual(delivered);
    });
	it('decodes one aggregate incoming record with canonical correlation and typed datums', () => {
		expect(decodeWire('ChatMessageWire', aggregateMessage, 'aggregate')).toEqual(aggregateMessage);
		for (const datum of aggregateDatums) expect(decodeWire('DatumWire', datum, 'aggregate/datum')).toEqual(datum);
		expect(() => decodeWire('DatumWire', { ...aggregateDatums[0], ok: 'true' }, 'bad-datum')).toThrow(WireDecodeError);
		expect(() => decodeWire('ChatMessageWire', { ...aggregateMessage, data: { ...aggregateMessage.data,
			sessionMessage: { ...aggregateMessage.data.sessionMessage, content: { ...aggregateMessage.data.sessionMessage.content,
				correlation: { request_id: 'aggregate-call' } } } } }, 'bad-correlation')).toThrow(WireDecodeError);
	});
	it('reports contract paths and keywords without revealing response content', () => {
		const { body, ...missing } = backendMessage;
		try {
			decodeWire('ChatMessageWire', { ...missing, data: { 'secret-dynamic-key': 'secret-prompt' } }, 'history');
			throw new Error('Expected invalid wire');
		} catch (error) {
			expect(error).toBeInstanceOf(WireDecodeError);
			expect((error as Error).message).toContain('[required] missing body');
			expect((error as Error).message).toContain('/required');
			expect((error as Error).message).not.toContain('secret');
		}
	});
 it.runIf(Boolean(process.env.THESIS_LIVE_CHAT_ID))('decodes the existing local chat snapshot through REST and SSE boundaries', async () => {
  const chatId = process.env.THESIS_LIVE_CHAT_ID!;
  const response = await fetch(`http://127.0.0.1:8000/v1/workspaces/test-ws-1/chats/${encodeURIComponent(chatId)}/timeline?limit=1000`,
   { headers: { Authorization: 'Bearer local-thesis-token' } });
  expect(response.ok).toBe(true);
  const page = decodeWire('TranscriptPageWire', await response.json(), 'live/timeline');
  expect(page.items.length).toBeGreaterThan(0);
  const canonical = await Promise.all(page.items.map(async item => {
   const detail = await fetch(`http://127.0.0.1:8000/v1/workspaces/test-ws-1/chats/${encodeURIComponent(chatId)}/timeline/items/${item.seq}`,
    { headers: { Authorization: 'Bearer local-thesis-token' } });
   expect(detail.ok).toBe(true);
   return decodeWire('TimelineItemWire', await detail.json(), 'live/timeline/detail');
  }));
  const bytes = new TextEncoder().encode(canonical.map((item) =>
   `id: ${item.seq}\nevent: timeline\ndata: ${JSON.stringify(item)}\n\n`).join(''));
   const stream = new TimelineStream({ baseUrl: '/api', workspaceId: 'test-ws-1', chatId, after: 0,
   fetchImpl: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes); } })) });
  const received: unknown[] = [];
  await stream.start((decoded) => { received.push(decoded); if (received.length === page.items.length) stream.stop(); });
  expect(received).toEqual(canonical);
 });
 it('accepts the persisted discovery seed delivery through history and live SSE', async () => {
  const delivered = {
   ok: true, failure: null, summary: 'Explore MSFT', text: 'Explore MSFT',
   datumType: 'agent_prompt', structured: { kind: 'agent_prompt', phase: 'discovery', attempt: 0 },
   kind: 'agent_prompt', phase: 'discovery', attempt: 0, subAgentId: 'discovery-agent',
   deliveryKey: 'phase-seed-discovery', activityKind: 'delivery'
  };
   const message = { ...backendMessage, role: 'user' as const, tool_result: delivered,
    conversation_id: 'discovery-agent', parent_conversation_id: backendMessage.chat_id,
   data: delivered };
  const item = { id: 'message:discovery-seed', chatId: message.chat_id,
   workspaceId: message.workspace_id, kind: 'message', seq: 9,
   ts: message.created_at, threadId: 'discovery-agent', payload: message };
   const previewItem = { ...item, payload: toTranscriptMessage(message) };
   expect(decodeWire('TranscriptPageWire', { items: [previewItem], nextBefore: 9, after: 9 }, 'history').items).toEqual([previewItem]);
  const bytes = new TextEncoder().encode(`id: 9\nevent: timeline\ndata: ${JSON.stringify(item)}\n\n`);
   const stream = new TimelineStream({ baseUrl: '/api', workspaceId: item.workspaceId, chatId: item.chatId, after: 0,
   fetchImpl: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes); } })) });
  const received: unknown[] = [];
  await stream.start((decoded) => { received.push(decoded); stream.stop(); });
  expect(received).toEqual([item]);
  expect(() => decodeWire('TimelineItemWire', { ...item, payload: { ...message,
   tool_result: { ok: true, datumType: 'agent_prompt' } } }, 'bad-delivery')).toThrow(WireDecodeError);
 });
	it('decodes actual Python serializer captures, including nullable tool summary and runtime metadata', () => {
		expect(decodeWire('ChatMessageWire', backendMessage, 'backend-capture')).toEqual(backendMessage);
		expect(decodeWire('ChatSubAgentTreeWire', backendTree, 'backend-capture')).toEqual(backendTree);
		expect(decodeWire('ChatThreadWire', backendThread, 'backend-capture')).toEqual(backendThread);
	});
	it.each(['conversation_id', 'root_conversation_id', 'parent_conversation_id'])('requires serialized ownership field %s', (field) => {
		for (const [schema, record] of [['ChatMessageWire', backendMessage], ['ChatThreadWire', backendThread]] as const) {
			const missing = Object.fromEntries(Object.entries(record).filter(([key]) => key !== field));
			expect(() => decodeWire(schema, missing, 'missing-ownership')).toThrow(WireDecodeError);
		}
	});
	it('requires a non-negative snapshot watermark even on an empty history page', () => {
		expect(decodeWire('TranscriptPageWire', { items: [], nextBefore: null, after: 0 }, 'empty-history').after).toBe(0);
		expect(() => decodeWire('TranscriptPageWire', { items: [], nextBefore: null }, 'missing-watermark')).toThrow(WireDecodeError);
		expect(() => decodeWire('TranscriptPageWire', { items: [], nextBefore: null, after: -1 }, 'invalid-watermark')).toThrow(WireDecodeError);
	});
	it('matches named response refs to the generated validators and preserves the required consumed map', async () => {
		const source = JSON.parse(await readFile('backend-wire.json', 'utf8'));
		const route = '/v1/workspaces/{workspace_id}/chats';
		const ref = (path: string, method: string) => source.paths[path][method].responses['200'].content['application/json'].schema.$ref;
		expect(ref(route, 'get')).toBe('#/components/schemas/ChatThreadListWire');
		expect(ref(route, 'post')).toBe('#/components/schemas/ChatThreadWire');
		expect(ref(`${route}/{chat_id}/messages`, 'post')).toBe('#/components/schemas/CreateMessageResponseWire');
		expect(ref(`${route}/{chat_id}/sub_agents/{sub_agent_id}/messages`, 'post')).toBe('#/components/schemas/NodeInputReceiptWire');
		expect(decodeWire('ChatThreadListWire', { items: [backendThread] }, 'capture').items[0].input_consumed_positions).toEqual({});
		const { input_consumed_positions, ...missing } = backendThread;
		expect(() => decodeWire('ChatThreadWire', missing, 'capture')).toThrow(WireDecodeError);
		expect(() => decodeWire('ChatThreadWire', { ...backendThread, input_consumed_positions: { root: '1' } }, 'capture')).toThrow(WireDecodeError);
	});
	it('validates full steering and operator admission provenance from the named schemas', () => {
		const steering = { intent: 'focus', targetsExecutionId: null, percolatedTo: [{ executionId: 'exec', intentEpoch: 2 }] };
		expect(decodeWire('CreateMessageResponseWire', { ...backendMessage, steering }, 'post').steering).toEqual(steering);
		expect(() => decodeWire('CreateMessageResponseWire', { ...backendMessage, steering: { ...steering, percolatedTo: [{ executionId: 'exec', intentEpoch: '2' }] } }, 'post')).toThrow(WireDecodeError);
		const operatorPolicy = { allowed: true, actorId: 'operator', actorKind: 'human_operator',
			allowedActorKinds: ['human_operator'], policy: 'human_only', role: 'researcher', nodeId: 'node' };
		const receipt = { ...backendMessage, conversation_id: 'node', parent_conversation_id: backendMessage.chat_id,
			data: { ...backendMessage.data, subAgentId: 'node' }, subAgentId: 'node', operatorPolicy };
		expect(decodeWire('NodeInputReceiptWire', receipt, 'node-post').operatorPolicy).toEqual(operatorPolicy);
		expect(() => decodeWire('NodeInputReceiptWire', { ...receipt, operatorPolicy: { allowed: true } }, 'node-post')).toThrow(WireDecodeError);
	});
	it('matches fresh generation from the authoritative backend export byte for byte', async () => {
		const source = JSON.parse(await readFile('backend-wire.json', 'utf8'));
		const generated = await generateWire([source]);
		expect(await readFile('src/lib/generated.ts', 'utf8')).toBe(generated.types);
		expect(await readFile('src/lib/wire-schema.json', 'utf8')).toBe(generated.schema);
	});

	it('validates the actual exported fixture API rather than a mocked API module', async () => {
		const chats = await listChats('alpha-lab');
		expect(chats.length).toBeGreaterThan(0);
		const history = await listTimelinePage('alpha-lab', chats[0].chat_id, null, null);
		expect(history.items.length).toBeGreaterThan(0);
		expect(history.after).toBe(Math.max(...history.items.map((item) => item.seq)));
		expect(decodeWire('TranscriptPageWire', history, 'fixture')).toEqual(history);
		const created = await createChat('wire-fixture');
		try {
			await postMessage('wire-fixture', created.chat_id, { body: 'offline wire message' });
			await postNodeMessage('wire-fixture', created.chat_id, { kind: 'sub_agent', id: 'node' }, { body: 'offline node message' });
			const page = await listTimelinePage('wire-fixture', created.chat_id, null, null);
			expect(page.items).toHaveLength(2);
			expect(page.after).toBe(Math.max(...page.items.map((item) => item.seq)));
			const scoped = await listTimelinePage('wire-fixture', created.chat_id, 'node', null);
			expect(scoped.items).toHaveLength(1);
			expect(scoped.after).toBe(page.after);
		expect(decodeWire('TranscriptPageWire', page, 'fixture')).toEqual(page);
		} finally { await deleteChat('wire-fixture', created.chat_id); }
	});

	it('rejects malformed SSE data before delivering it or advancing the cursor', async () => {
		const bytes = new TextEncoder().encode('id: 7\nevent: timeline.item\ndata: {"seq":7,"kind":"message","payload":{}}\n\n');
		const delivered: unknown[] = [];
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: 'ws', chatId: 'chat', after: 0,
			fetchImpl: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } })) });
		await expect(stream.start((item) => delivered.push(item))).rejects.toBeInstanceOf(WireDecodeError);
		expect(delivered).toEqual([]);
		stream.stop();
	});
	it('delivers a genuine backend message through the real SSE decoder unchanged', async () => {
		const item = { id: backendMessage.message_id, chatId: backendMessage.chat_id, workspaceId: backendMessage.workspace_id,
			kind: 'message', seq: 7, ts: backendMessage.created_at, threadId: 'root', payload: backendMessage };
		const bytes = new TextEncoder().encode(`id: 7\nevent: timeline.item\ndata: ${JSON.stringify(item)}\n\n`);
		const delivered: unknown[] = [];
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: item.workspaceId, chatId: item.chatId, after: 0,
			fetchImpl: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes); } })) });
		await stream.start((decoded) => { delivered.push(decoded); stream.stop(); });
		expect(delivered).toEqual([item]);
	});
	it('validates nested message and tool payloads, not only timeline envelopes', async () => {
		const page = await listTimelinePage('alpha-lab', 'chat-1', 'root', null);
		const message = page.items.find((item) => item.kind === 'message')!;
		expect(() => decodeWire('TimelineItemWire', { ...message, payload: { ...message.payload, role: 'invented_role' } }, 'test')).toThrow(WireDecodeError);
		expect(() => decodeWire('TimelineItemWire', { ...message, payload: { ...message.payload,
			tool_result: { ok: true, tool: 'run_backtest' } } }, 'test')).toThrow(WireDecodeError);
	});
	it('validates tool outcomes inside open execution event payloads at the same decoder boundary', () => {
		const event = { actorId: 'agent', actorScope: 'tool', correlationId: 'call', cursor: 1, eventId: 'event',
			eventType: 'tool.result', executionId: 'exec', intentEpoch: 0, lateResult: false,
			occurredAt: '2026-01-01T00:00:00Z', workspaceId: 'workspace', payload: { toolResult: backendMessage.tool_result } };
		expect(decodeWire('ExecutionEventWire', event, 'test')).toEqual(event);
		expect(() => decodeWire('ExecutionEventWire', { ...event, payload: { toolResult: { ok: true } } }, 'test')).toThrow(WireDecodeError);
	});
});
