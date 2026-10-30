import { expect, test } from '@playwright/test';
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import messages from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import workspaces from '../src/lib/fixtures/workspaces.json' with { type: 'json' };
import snapshots from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import { toTranscriptMessage } from '../src/lib/transcript';

test('previews load exact evidence on demand and conversation revisits resume the outline cursor', async ({ page }) => {
 test.setTimeout(30_000);
 const workspace = workspaces.items.find(item => item.id === 'alpha-lab')!;
 const first = chats['alpha-lab'].items[0];
 const second = { ...first, chat_id: 'read-second', conversation_id: 'read-second', root_conversation_id: 'read-second', title: 'Another conversation' };
 const full = { ...messages['chat-1'].items[0], message_id: 'large-message', chat_id: first.chat_id,
  conversation_id: first.chat_id, root_conversation_id: first.chat_id, parent_conversation_id: null,
  body: 'Full retained evidence. '.repeat(1000) + 'Exact evidence ending.', execution_id: null };
 const preview = { ...toTranscriptMessage(full), body: 'A bounded message preview.', detailsAvailable: true };
 const evidence = 'Returned artifact evidence. '.repeat(1000) + 'Exact artifact ending.';
 const toolResult = { datumType: 'read_artifact', tool: 'read_artifact', callId: 'inspect-artifact', operation: 'read',
  ok: true, summary: 'Read metrics', text: evidence, structured: { name: 'metrics.json', content: evidence }, failure: null };
 const fullTool = { ...full, message_id: 'tool-evidence', role: 'tool', body: 'Read metrics', tool_result: toolResult,
  data: { activityKind: 'delivery', sessionMessage: { message_id: 'tool-evidence', direction: 'incoming', position: 101,
   content: { text: 'Read metrics', source: 'tool_result', requests: [], correlation: null, result: toolResult } } } };
 const thinResult = { ...toolResult, text: '', structured: { name: 'metrics.json' }, detailsAvailable: true };
 const toolPreview = { ...toTranscriptMessage(fullTool), tool_result: thinResult,
  receipt: { requests: [], correlation: null, result: thinResult }, detailsAvailable: true };
 const outlineAfter: number[] = [];
 const detailReads: string[] = [];
 let historyReads = 0;
 await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
 await page.route('**/api/**', async route => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  if (path.endsWith('/timeline/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
  if (path.includes('/timeline/items/')) {
   detailReads.push(path);
   const tool = path.endsWith('/101');
   return route.fulfill({ json: { id: tool ? 'retained-tool' : 'retained-item', kind: 'message', seq: tool ? 101 : 100, threadId: 'root', chatId: first.chat_id,
    workspaceId: workspace.id, ts: full.created_at, payload: tool ? fullTool : full } });
  }
  if (path.endsWith('/timeline')) {
   historyReads++;
   return route.fulfill({ json: { items: path.includes(first.chat_id) ? [{ id: 'retained-item', kind: 'message', seq: 100,
    threadId: 'root', chatId: first.chat_id, workspaceId: workspace.id, ts: full.created_at, payload: preview },
    { id: 'retained-tool', kind: 'message', seq: 101, threadId: 'root', chatId: first.chat_id, workspaceId: workspace.id,
     ts: full.created_at, payload: toolPreview }] : [], nextBefore: null, after: 101 } });
  }
  if (path.endsWith('/outline')) {
   if (path.includes(first.chat_id)) outlineAfter.push(Number(url.searchParams.get('after')));
   return route.fulfill({ json: { items: [], after: 101, nextAfter: null } });
  }
  if (path.endsWith('/sub_agents')) return route.fulfill({ json: { chatId: path.includes(first.chat_id) ? first.chat_id : second.chat_id, items: [] } });
  if (path.endsWith('/executions')) return route.fulfill({ json: { items: [], after: 100 } });
  if (path.endsWith('/chats')) return route.fulfill({ json: { items: [first, second] } });
  if (path.endsWith('/workspaces')) return route.fulfill({ json: { items: [workspace] } });
  if (path.endsWith(`/workspaces/${workspace.id}`)) return route.fulfill({ json: workspace });
  if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshots['alpha-lab'] });
  return route.fulfill({ status: 404, json: { detail: 'Not part of this scenario' } });
 });
 await page.goto(`/w/${workspace.id}/conversations?chat=${first.chat_id}`);
 await expect(page.getByText('A bounded message preview.', { exact: true })).toBeVisible();
 expect(detailReads).toHaveLength(0);
 await expect(page.getByText(/Exact evidence ending/)).toHaveCount(0);
 await page.getByTestId('message-details').click();
 await expect(page.getByText(/Exact evidence ending/)).toBeVisible();
 await expect(page.getByTestId('message-details')).toHaveCount(0);
 expect(detailReads).toHaveLength(1);
 await page.locator('.activity-receipt > summary').click();
 await expect.poll(() => detailReads.length).toBe(2);
 await expect(page.getByText(/Exact artifact ending/).first()).toBeVisible();
 await page.locator(`[data-testid="conversation-item"][data-chat="${second.chat_id}"]`).click();
 await expect(page.getByText(/Exact evidence ending/)).toHaveCount(0);
 await page.locator(`[data-testid="conversation-item"][data-chat="${first.chat_id}"]`).click();
 await expect(page.getByText('A bounded message preview.', { exact: true })).toBeVisible();
 await expect.poll(() => outlineAfter.length).toBeGreaterThan(1);
 expect(outlineAfter.filter(after => after === 0)).toHaveLength(1);
 const before = historyReads;
 await page.locator(`[data-testid="conversation-item"][data-chat="${first.chat_id}"]`).click();
 expect(historyReads).toBe(before);
});
