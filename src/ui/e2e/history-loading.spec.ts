import { expect, test } from '@playwright/test';
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import executions from '../src/lib/fixtures/executions.json' with { type: 'json' };
import messages from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import workspaces from '../src/lib/fixtures/workspaces.json' with { type: 'json' };
import snapshot from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import backendTree from '../src/tests/backend-tree.json' with { type: 'json' };

test('recent selected-thread history opens immediately and older history loads on request', async ({ page }) => {
    test.setTimeout(30_000);
    const chat = chats['alpha-lab'].items[0];
    const workspace = workspaces.items.find((item) => item.id === 'alpha-lab')!;
    const requests: URL[] = [];
    const row = (seq: number, body: string, thread = 'root') => ({
        id: `history-${thread}-${seq}`, kind: 'message', seq, threadId: thread,
        chatId: chat.chat_id, workspaceId: workspace.id, ts: chat.created_at,
        payload: { ...messages['chat-1'].items[0], message_id: `history-${thread}-${seq}`,
            body, chat_id: chat.chat_id, conversation_id: thread === 'root' ? chat.chat_id : thread,
            root_conversation_id: chat.chat_id, data: thread === 'root' ? null : { subAgentId: thread } }
    });
    const node = { ...backendTree.items[0], subAgentId: 'selected-child', chatId: chat.chat_id,
        parentSubAgentId: null, returnsToSubAgentId: null, role: 'History helper',
        workflowExecutionId: null, phase: null, phaseExecutor: false, phaseAttemptId: null, children: [] };
    await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
    await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        if (path.endsWith('/timeline/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
        if (path.endsWith('/timeline')) {
            requests.push(url);
            const child = url.searchParams.get('thread') === 'selected-child';
            const older = url.searchParams.has('before');
            return route.fulfill({ json: { items: [child ? row(300, 'Selected child history', 'selected-child') : older ? row(1, 'Earlier root history') : row(500, 'Recent root history')], nextBefore: child || older ? null : 500, after: 500 } });
        }
        if (path.endsWith('/executions')) return route.fulfill({ json: { items: [{ ...executions['chat-1'], conversation_id: 'selected-child' }], after: 500 } });
        if (path.endsWith('/workflows/wf-strategy-tune')) return route.fulfill({ json: { workflow_id: 'wf-strategy-tune', version: '1.4.0', title: 'Nested metadata', phases: ['backtest', 'completed'], terminal_phases: ['completed'], transitions: { backtest: ['completed'], completed: [] }, splits: {} } });
        if (path.endsWith('/sub_agents')) return route.fulfill({ json: { chatId: chat.chat_id, roots: [node], items: [node] } });
        if (path.endsWith('/chats')) return route.fulfill({ json: { items: [chat] } });
        if (path.endsWith(`/chats/${chat.chat_id}`)) return route.fulfill({ json: chat });
        if (path.endsWith('/workspaces')) return route.fulfill({ json: { items: [workspace] } });
        if (path.endsWith(`/workspaces/${workspace.id}`)) return route.fulfill({ json: workspace });
        if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshot['alpha-lab'] });
        return route.fulfill({ status: 404, json: { detail: 'Not part of this scenario' } });
    });
    const base = `/w/${workspace.id}/conversations?chat=${chat.chat_id}`;
    await page.goto(base);
    await expect(page.getByText('Recent root history', { exact: true })).toBeVisible();
    await expect(page.getByTestId('history-older')).toBeVisible();
    await expect(page.getByTestId('workflow-card')).toHaveCount(0);
    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.get('thread')).toBe('root');
    expect(requests[0].searchParams.get('limit')).toBe('200');
    await page.getByTestId('history-older').click();
    await expect(page.getByText('Earlier root history', { exact: true })).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(requests[1].searchParams.get('before')).toBe('500');
    await expect(page.getByTestId('history-older')).toHaveCount(0);
    await page.goto(`${base}&scope=${encodeURIComponent(`conversation:${chat.chat_id}/sub_agent:selected-child`)}`);
    await expect(page.getByText('Selected child history', { exact: true })).toBeVisible();
    await expect(page.getByTestId('workflow-card')).toHaveCount(1);
    expect(requests.filter((url) => url.searchParams.get('thread') === 'selected-child')).toHaveLength(1);
    expect(requests.every((url) => ['root', 'selected-child'].includes(url.searchParams.get('thread') ?? ''))).toBe(true);
});
