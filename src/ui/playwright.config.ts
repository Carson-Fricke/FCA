import { defineConfig } from '@playwright/test';

/**
 * End-to-end tests against the real local stack: the running Thesis API
 * (port 8000) and the Vite dev server (started by webServer on 5173, which
 * proxies `/api/*` to the Thesis API with the bearer injected server-side).
 *
 * The stack is started externally via `scripts/local/start-stack.ps1`; these
 * tests assume it is up. Every test creates its own chat, so runs are
 * independent and re-runnable.
 */
export default defineConfig({
	testDir: './e2e',
	timeout: 300_000,
	expect: { timeout: 60_000 },
	fullyParallel: false,
	workers: 1,
	reporter: [['list']],
	use: {
		baseURL: 'http://127.0.0.1:5173',
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'chromium', use: { browserName: 'chromium' } }]
});
