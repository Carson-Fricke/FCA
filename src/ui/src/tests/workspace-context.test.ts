import { describe, expect, it } from 'vitest';
import { fixtureMode, getWorkspace } from '$lib/api';
import { placeholderSnapshot } from '$lib/workspace-context.svelte';

describe('workspace render gate', () => {
	it('returns the workspace summary without counts or activity', async () => {
		expect(fixtureMode).toBe(true);
		const summary = await getWorkspace('alpha-lab');
		expect(summary.id).toBe('alpha-lab');
		expect(summary.name).toBeTruthy();
		expect('counts' in summary).toBe(false);
		expect('overview' in summary).toBe(false);
	});

	it('rejects an unknown workspace', async () => {
		await expect(getWorkspace('does-not-exist')).rejects.toThrow();
	});

	it('builds placeholder shell state with zero counts and no activity', () => {
		const placeholder = placeholderSnapshot({
			id: 'alpha-lab',
			name: 'Alpha Lab',
			description: '',
			initials: 'AL',
			created_at: '2026-01-01T00:00:00Z'
		});
		expect(placeholder.workspaces.map((item) => item.id)).toEqual(['alpha-lab']);
		expect(placeholder.overview.activity).toEqual([]);
		expect(placeholder.overview.openChats).toEqual([]);
		expect(placeholder.counts).toEqual({
			strategies: 0,
			chats: 0,
			backtests: 0,
			deployments: 0,
			dashboards: 0,
			screeners: 0
		});
	});
});
