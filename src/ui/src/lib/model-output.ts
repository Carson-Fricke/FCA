/** Rebuild provisional output from durable, idempotent UTF-8-offset fragments. */
import type { TimelineItemWire, TimelineOutputWire } from './generated';
import type { ChatMessage } from './types';

export function projectModelOutputs(items: TimelineOutputWire[]): ChatMessage[] {
    const groups = new Map<string, TimelineOutputWire[]>();
    for (const item of items) {
        const group = groups.get(item.payload.messageId) ?? [];
        group.push(item);
        groups.set(item.payload.messageId, group);
    }
    const encoder = new TextEncoder();
    return [...groups].map(([messageId, group]) => {
        group.sort((a, b) => a.seq - b.seq);
        const first = group.find((item) => item.payload.state === 'started') ?? group[0];
        const threadId = first.threadId ?? 'root';
        const interrupted = group.find((item) => item.payload.state === 'interrupted');
        const channel = (name: 'reasoning' | 'text') => {
            const fragments = new Map<number, string>();
            for (const item of group) {
                if (item.payload.state === 'delta' && item.payload.channel === name)
                    fragments.set(item.payload.offset, item.payload.text);
            }
            let offset = 0;
            const parts: string[] = [];
            for (const [position, text] of [...fragments].sort(([a], [b]) => a - b)) {
                if (position !== offset) break; // Backfill must fill gaps before text is shown.
                parts.push(text);
                offset += encoder.encode(text).length;
            }
            return parts.join('');
        };
        return {
            message_id: messageId, chat_id: first.chatId, workspace_id: first.workspaceId,
            actor_id: first.payload.actorId, role: 'assistant', body: channel('text'),
            reasoning: channel('reasoning'), state: 'accepted', attachment_ids: [],
            created_at: first.ts, updated_at: null, execution_id: first.executionId ?? null,
            intent_epoch: 0, kind: threadId === 'root' ? 'message' : 'sub_agent',
            threadId, seq: first.seq,
            outputState: interrupted ? 'interrupted' : 'streaming',
            outputFailure: interrupted?.payload.state === 'interrupted'
                ? String(interrupted.payload.failure.message ?? 'Model execution was interrupted.') : undefined,
            data: threadId === 'root' ? null : { subAgentId: threadId }
        } satisfies ChatMessage;
    });
}

/** Completed invocations stop accumulating/reprojecting when later history arrives. */
export class ModelOutputProjection {
    #completed = new Set<string>();
    #frames = new Map<string, Map<string, TimelineOutputWire>>();
    #messages = new Map<string, ChatMessage>();

    merge(items: TimelineItemWire[]): ChatMessage[] {
        const changed = new Set<string>();
        for (const item of items) {
            if (item.kind === 'message') {
                const id = item.payload.message_id;
                this.#completed.add(id);
                this.#frames.delete(id);
                this.#messages.delete(id);
            } else if (item.kind === 'output' && !this.#completed.has(item.payload.messageId)) {
                const id = item.payload.messageId;
                const frames = this.#frames.get(id) ?? new Map<string, TimelineOutputWire>();
                frames.set(item.id, item);
                this.#frames.set(id, frames);
                changed.add(id);
            }
        }
        for (const id of changed) {
            const frames = this.#frames.get(id);
            if (frames) this.#messages.set(id, projectModelOutputs([...frames.values()])[0]);
        }
        return [...this.#messages.values()];
    }
}
