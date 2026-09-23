const { chromium } = require('@playwright/test');

(async () => {
	const b = await chromium.launch();
	const p = await b.newPage();
	await p.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
	await p.goto('http://127.0.0.1:5173/w/test-ws-1/conversations');
	await p.getByTestId('new-conversation').click();
	await p
		.locator('[data-testid="chat-composer"] textarea')
		.fill(
			'I think SPY momentum persists over the last month. Start the hypothesis development workflow.'
		);
	await p.getByTestId('composer-send').click();
	for (let i = 0; i < 15; i++) {
		await p.waitForTimeout(20000);
		const status = await p
			.getByTestId('workflow-card-status')
			.textContent()
			.catch(() => '?');
		const phases = await p
			.locator('[data-testid="phase-step"]')
			.evaluateAll((els) =>
				els.map((e) => `${e.getAttribute('data-phase')}:${e.getAttribute('data-status')}`)
			);
		const attention = await p.getByTestId('workflow-card-needs-input').count();
		console.log(
			i,
			'status:',
			String(status).trim(),
			'attention:',
			attention,
			'phases:',
			JSON.stringify(phases)
		);
		if (phases.some((s) => s && s.startsWith('verdict:active') || (s && s.includes('verdict:done')))) break;
	}
	await b.close();
})().catch((e) => {
	console.error(String(e).slice(0, 600));
	process.exit(1);
});
