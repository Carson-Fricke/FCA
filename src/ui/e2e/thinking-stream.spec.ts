import { transcriptItems, nodeSummaries } from './read-fixtures';
import { test, expect } from '@playwright/test';
import { send, WORKSPACE_ID } from './fixtures';
import seed from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import snapshots from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import type { TimelineItemWire } from '../src/lib/generated';

test('thinking grows while collapsed, restores from history, and keeps expansion on completion', async ({ page }) => {
    const frames: TimelineItemWire[] = [];
    let chatId = '';
    const now = '2026-10-12T18:00:00Z';
    const messageId = 'streamed-answer';
    const envelope = (seq: number) => ({ id: `output-${seq}`, chatId, workspaceId: WORKSPACE_ID,
        seq, ts: now, threadId: 'root' });
    await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
    const workspace = { ...snapshots['alpha-lab'].overview.workspace, id: WORKSPACE_ID };
    const thread = { ...chats['alpha-lab'].items[0], chat_id: 'thinking-stream-chat',
        conversation_id: 'thinking-stream-chat', root_conversation_id: 'thinking-stream-chat',
        workspace_id: WORKSPACE_ID, title: 'Thinking stream' };
    let created = false;
    await page.route('**/api/v1/workspaces**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (route.request().method() === 'DELETE') return route.fulfill({ status: 204 });
        if (path === '/api/v1/workspaces') return route.fulfill({ json: { items: [workspace] } });
        if (path.endsWith(`/${WORKSPACE_ID}`)) return route.fulfill({ json: workspace });
        if (path.endsWith('/snapshot')) return route.fulfill({ json: {
            ...snapshots['alpha-lab'], overview: { ...snapshots['alpha-lab'].overview, workspace }
        } });
        if (path.endsWith('/chats')) {
            if (route.request().method() === 'POST') {
                created = true;
                return route.fulfill({ json: thread });
            }
            return route.fulfill({ json: { items: created ? [thread] : [] } });
        }
        if (path.endsWith(`/${thread.chat_id}`)) return route.fulfill({ json: thread });
        return route.fulfill({ json: { items: [] } });
    });
    await page.route('**/chats/*/timeline?**', async (route) => {
        await route.fulfill({ json: { items: transcriptItems(frames), after: frames.at(-1)?.seq ?? 0, nextBefore: null } });
    });
    await page.route('**/chats/*/timeline/stream?**', async (route) => {
        const after = Number(new URL(route.request().url()).searchParams.get('after') ?? 0);
        const body = frames.filter((frame) => frame.seq > after).map((frame) =>
            `id: ${frame.seq}\nevent: timeline\ndata: ${JSON.stringify(frame)}\n\n`).join('');
        await route.fulfill({ contentType: 'text/event-stream', body });
    });
    await page.route('**/chats/*/messages', async (route) => {
        if (route.request().method() !== 'POST') return route.continue();
        chatId = route.request().url().split('/chats/')[1].split('/')[0];
        const input = route.request().postDataJSON();
        frames.push({ ...envelope(1), kind: 'output', payload: {
            state: 'started', invocationId: 'invocation', messageId, actorId: 'agent', startedAt: now
        } });
        frames.push({ ...envelope(2), kind: 'output', payload: {
            state: 'delta', invocationId: 'invocation', messageId, actorId: 'agent',
            channel: 'reasoning', offset: 0, text: 'First thought.'
        } });
        await route.fulfill({ json: { ...seed['chat-1'].items[0], message_id: input.message_id,
            chat_id: chatId, workspace_id: WORKSPACE_ID, conversation_id: chatId,
            root_conversation_id: chatId, body: input.body, created_at: now, execution_id: null } });
    });
    await page.goto(`/w/${WORKSPACE_ID}/conversations`);
    await page.getByTestId('new-conversation').click();
    try {
        await send(page, 'Streaming test');
        const thinking = page.getByTestId(`thinking-${messageId}`);
        await expect(thinking.getByRole('button')).toHaveText('Thinking…');
        await expect(thinking.getByTestId(`thinking-${messageId}-body`)).toHaveCount(0);
        await thinking.getByRole('button').click();
        await expect(thinking).toContainText('First thought.');
        frames.push({ ...envelope(3), kind: 'output', payload: {
            state: 'delta', invocationId: 'invocation', messageId, actorId: 'agent',
            channel: 'reasoning', offset: 14, text: ' Second thought.'
        } });
        await expect(thinking).toContainText('First thought. Second thought.');
        await page.reload();
        await expect(thinking.getByRole('button')).toHaveText('Thinking…');
        await thinking.getByRole('button').click();
        await expect(thinking).toContainText('First thought. Second thought.');
        frames.push({ ...envelope(4), kind: 'message', payload: {
            ...seed['chat-1'].items[1], message_id: messageId, workspace_id: WORKSPACE_ID,
            chat_id: chatId, conversation_id: chatId, root_conversation_id: chatId,
            actor_id: 'agent', body: 'Final answer.', reasoning: 'First thought. Second thought.',
            role: 'assistant', kind: 'message', created_at: now, data: null, execution_id: null
        } });
        await expect(thinking.getByRole('button')).toHaveText('Thinking');
        await expect(thinking.getByRole('button')).toHaveAttribute('aria-expanded', 'true');
        await expect(page.locator(`[data-message-id="${messageId}"]`)).toHaveCount(1);
        await expect(page.getByText('Final answer.', { exact: true })).toBeVisible();
    } finally {
        if (chatId) await page.request.delete(`/api/v1/workspaces/${WORKSPACE_ID}/chats/${chatId}`);
    }
});
