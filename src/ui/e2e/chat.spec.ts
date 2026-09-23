import { expect, send, test } from './fixtures';

test.describe('chat surface', () => {
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
