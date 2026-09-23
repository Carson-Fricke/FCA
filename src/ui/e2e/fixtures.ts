import {
	expect,
	test as base,
	type Page
} from '@playwright/test';

/**
 * E2E against the real local stack: the running Thesis API (port 8000,
 * restarted with the refactored code) and the Vite dev server on 5173
 * (same-origin proxy, bearer injected server-side).
 *
 * The workspace agent replies through the live model, so every assertion
 * waits on durable UI outcomes. Timings are generous: proposal turns took
 * tens of seconds in live runs, and the trials phase runs the research
 * model through the harness.
 */

export const CONVERSATIONS_URL = '/w/test-ws-1/conversations';

export const test = base.extend<{ chatPage: Page }>({
	chatPage: async ({ page }, use) => {
		// A fresh browser profile has no stored backend choice, and the app
		// resolves to fixture mode without one - seed the local backend before
		// the app loads so `$lib/api` talks to the real stack.
		await page.addInitScript(() => {
			localStorage.setItem('thesis.backend', 'local');
		});
		await page.goto(CONVERSATIONS_URL);
		await expect(page.getByTestId('chat-surface')).toBeVisible();
		// Start from a brand-new conversation every time, so runs are
		// independent and re-runnable.
		await page.getByTestId('new-conversation').click();
		await expect(page.getByTestId('chat-composer')).toBeVisible();
		await use(page);
	}
});

export { expect };

/** Open the composer, type, send. */
export async function send(page: Page, body: string): Promise<void> {
	await page.locator('[data-testid="chat-composer"] textarea').fill(body);
	await page.getByTestId('composer-send').click();
}

/** The workflow card for the launch this test created. */
export function workflowCard(page: Page) {
	return page.getByTestId('workflow-card');
}
