import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import WorkspaceSwitcher from '$lib/components/shell/WorkspaceSwitcher.svelte';
import { fixtures } from '$lib/fixtures';
import { pinnedWidgets, togglePin, ui } from '$lib/workspace.svelte';
import type { WorkspaceSummary } from '$lib/types';

const workspaces = structuredClone(fixtures.workspaces.items) as WorkspaceSummary[];

describe('Workspace switcher', () => {
	it('lists accessible workspaces and switches scope on selection', async () => {
		const onSwitch = vi.fn();
		const { container } = render(WorkspaceSwitcher, {
			props: { workspaces, currentId: 'alpha-lab', onSwitch, onCreate: vi.fn() }
		});
		await tick();

		// Popover opens on demand, showing all accessible workspaces.
		expect(screen.queryByTestId('workspace-menu')).toBeNull();
		await fireEvent.click(screen.getByTestId('workspace-switcher-button'));
		await tick();
		expect(screen.getByTestId('workspace-menu')).toBeInTheDocument();

		const options = container.querySelectorAll('[data-testid="workspace-option"]');
		expect(options).toHaveLength(workspaces.length);

		// Selecting another workspace re-scopes: the callback carries the id
		// and the route change re-runs every workspace-scoped load.
		const beta = container.querySelector(
			'[data-testid="workspace-option"][data-workspace="beta-quant"]'
		);
		expect(beta).not.toBeNull();
		await fireEvent.click(beta as HTMLElement);
		await tick();
		expect(onSwitch).toHaveBeenCalledWith('beta-quant');
	});

	it('marks the current workspace and exposes create-workspace', async () => {
		const onCreate = vi.fn();
		render(WorkspaceSwitcher, {
			props: { workspaces, currentId: 'beta-quant', onSwitch: vi.fn(), onCreate }
		});
		await tick();
		await fireEvent.click(screen.getByTestId('workspace-switcher-button'));
		await tick();
		// The workspace name shows in the collapsed button and in the menu.
		expect(screen.getAllByText('Systematic futures studies').length).toBeGreaterThanOrEqual(2);
		await fireEvent.click(screen.getByTestId('create-workspace-trigger'));
		await tick();
		expect(onCreate).toHaveBeenCalledTimes(1);
	});
});

describe('Workspace-scoped view state', () => {
	it('keeps pinned widgets scoped per workspace and re-scopes on switch', () => {
		expect(ui.pinnedWidgets['alpha-lab']).toBeUndefined();
		togglePin('alpha-lab', 'wgt-1');
		togglePin('beta-quant', 'wgt-b1');
		expect(pinnedWidgets('alpha-lab')).toEqual(['wgt-1']);
		expect(pinnedWidgets('beta-quant')).toEqual(['wgt-b1']);

		// Switching workspaces re-scopes the pinned strip: beta's pins do not
		// leak into alpha-lab's sidebar.
		togglePin('alpha-lab', 'wgt-1');
		expect(pinnedWidgets('alpha-lab')).toEqual([]);
		expect(pinnedWidgets('beta-quant')).toEqual(['wgt-b1']);
	});
});
