import { describe, expect, it } from 'vitest';
import { COPILOT_MIN_WIDTH, setCopilotWidth, ui } from '$lib/workspace.svelte';

describe('Copilot pane width', () => {
	it('clamps a width below the minimum up to the minimum', () => {
		setCopilotWidth(120);
		expect(ui.copilotWidth).toBe(COPILOT_MIN_WIDTH);
	});

	it('persists the settled width', () => {
		setCopilotWidth(420);
		expect(ui.copilotWidth).toBe(420);
		expect(localStorage.getItem('thesis.copilotWidth')).toBe('420');
	});
});
