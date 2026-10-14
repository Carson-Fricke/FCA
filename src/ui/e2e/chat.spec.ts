import { expect, send, test, WORKSPACE_ID } from './fixtures';
import messages from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import { test as browserTest } from '@playwright/test';

test.describe('chat surface', () => {
	browserTest('shows a pending send and clears the composer before the HTTP response', async ({ page }) => {
		browserTest.setTimeout(60_000);
		await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
		await page.goto(`/w/${WORKSPACE_ID}/conversations`);
		await page.getByTestId('new-conversation').click();
		const bubble = page.locator('[data-role="user"]').filter({ hasText: 'pending browser send' });
		let release!: () => void;
		let admittedChat = '';
		const gate = new Promise<void>((resolve) => { release = resolve; });
		await page.route('**/chats/*/messages', async (route) => {
			if (route.request().method() !== 'POST') return route.continue();
			admittedChat = route.request().url().split('/chats/')[1].split('/')[0];
			const input = route.request().postDataJSON();
			await gate;
			await route.fulfill({ json: { ...messages['chat-1'].items[0],
				workspace_id: WORKSPACE_ID, chat_id: admittedChat, conversation_id: admittedChat,
				root_conversation_id: admittedChat, message_id: input.message_id, body: input.body,
				execution_id: null, input_position: 1, created_at: new Date().toISOString() } });
		});
		try {
			await send(page, 'pending browser send');
			await expect(page.getByTestId('composer-input')).toHaveValue('');
			await expect(bubble).toHaveCount(1);
			await expect(page.getByText(/Sending/)).toBeVisible();
			await page.getByTestId('composer-input').fill('newer browser draft');
			release();
			await expect(page.getByText(/Sending/)).toHaveCount(0);
			await expect(bubble).toHaveCount(1);
			await expect(page.getByTestId('composer-input')).toHaveValue('newer browser draft');
		} finally {
			release();
			if (admittedChat) await page.request.delete(`/api/v1/workspaces/${WORKSPACE_ID}/chats/${admittedChat}`);
		}
	});

	test('renders the sent message and the durable model reply', async ({ chatPage }) => {
		const page = chatPage;
		await send(page, 'Reply with exactly: e2e-ok');

		// The user bubble is durable immediately.
		await expect(page.locator('[data-role="user"]').last()).toHaveText(/e2e-ok/);

		// The reply arrives through the shared timeline stream as a durable
		// assistant message from the workspace agent.
		await expect(
			page.locator('[data-role="assistant"][data-message-id]').last()
		).toBeVisible({ timeout: 180_000 });
	});
});
