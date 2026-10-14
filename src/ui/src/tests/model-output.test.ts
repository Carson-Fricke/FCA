/** Ordering cases that require overlapping history pages and UTF-8 offsets. */
import { describe, expect, it } from 'vitest';
import { projectModelOutputs } from '$lib/model-output';
import type { TimelineOutputWire } from '$lib/generated';
import { threadActivity } from '$lib/transcript';
import type { ChatMessage } from '$lib/types';

function delta(seq: number, offset: number, text: string, threadId = 'root'): TimelineOutputWire {
    return { id: `delta-${seq}`, kind: 'output', seq, ts: '2026-01-01T00:00:00Z',
        chatId: 'chat', workspaceId: 'workspace', threadId,
        payload: { state: 'delta', invocationId: threadId, messageId: `message-${threadId}`,
            actorId: 'agent', channel: 'reasoning', offset, text } };
}

describe('durable provisional output', () => {
    it('waits for missing history and joins by UTF-8 bytes rather than UTF-16 length', () => {
        const tail = delta(3, 4, ' after');
        expect(projectModelOutputs([tail])[0].reasoning).toBe('');
        const head = delta(2, 0, '🧠');
        expect(projectModelOutputs([tail, head, head])[0].reasoning).toBe('🧠 after');
    });

    it('keeps root and phase invocations separate and retains interrupted evidence', () => {
        const phase = delta(2, 0, 'Phase thought', 'phase');
        const interrupted: TimelineOutputWire = { ...phase, id: 'interrupted', seq: 3,
            payload: { state: 'interrupted', invocationId: 'phase', messageId: 'message-phase',
                actorId: 'agent', failure: { message: 'Connection lost' } } };
        const outputs = projectModelOutputs([interrupted, delta(1, 0, 'Root thought'), phase]);
        expect(outputs.find((output) => output.threadId === 'root')?.reasoning).toBe('Root thought');
        expect(outputs.find((output) => output.threadId === 'phase')).toMatchObject({
            reasoning: 'Phase thought', outputState: 'interrupted', outputFailure: 'Connection lost'
        });
    });

    it('ends an interrupted attempt without declaring a reply or hiding a later queued input', () => {
        const partial = projectModelOutputs([delta(2, 0, 'Partial')])[0];
        partial.outputState = 'interrupted';
        const input: ChatMessage = { ...partial, message_id: 'input', seq: 1,
            role: 'user', body: 'Question', reasoning: null, outputState: undefined };
        expect(threadActivity([input, partial], [], false, 'input')).toMatchObject({ pending: false, queued: true });
        const later = { ...input, message_id: 'later', seq: 3 };
        expect(threadActivity([input, partial, later], [], false, 'later')).toMatchObject({ pending: true, queued: true });
    });
});
