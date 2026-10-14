import { expect, test } from '@playwright/test';

test('deletes the current workspace with confirmation, retry, and no stale selection', async ({ page }) => {
    await page.addInitScript(() => {
        localStorage.setItem('thesis.backend', 'local');
        localStorage.removeItem('thesis.workspace');
    });
    const created = await page.request.post('/api/v1/workspaces', {
        data: { name: `Delete workspace ${Date.now()}`, description: 'Deletion behavior test' }
    });
    expect(created.ok()).toBeTruthy();
    const workspace = await created.json();
    const path = `/api/v1/workspaces/${workspace.id}`;
    try {
        const chat = await page.request.post(`${path}/chats`, { data: { title: 'Delete my history', owner_actor_id: 'operator' } });
        expect(chat.ok()).toBeTruthy();
        await page.goto(`/w/${workspace.id}/overview`);
        await page.getByTestId('workspace-switcher-button').click();
        await page.getByTestId('delete-workspace-trigger').click();
        const dialog = page.getByRole('dialog', { name: 'Delete workspace', exact: true });
        await expect(dialog).toContainText('all its conversations');
        await expect(page.getByTestId('workspace-delete-submit')).toBeDisabled();
        await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
        expect((await page.request.get(path)).status()).toBe(200);

        await page.getByTestId('workspace-switcher-button').click();
        await page.getByTestId('delete-workspace-trigger').click();
        await page.getByTestId('workspace-delete-confirmation').fill(workspace.name);
        let rejectOnce = true;
        await page.route(`**${path}`, async (route) => {
            if (route.request().method() === 'DELETE' && rejectOnce) {
                rejectOnce = false;
                await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Try deletion again' }) });
            } else await route.continue();
        });
        await page.getByTestId('workspace-delete-submit').click();
        await expect(dialog.getByRole('alert')).toContainText('Try deletion again');
        expect(await page.evaluate(() => localStorage.getItem('thesis.workspace'))).toBe(workspace.id);
        await page.getByTestId('workspace-delete-submit').click();
        await expect(page).toHaveURL(/\/w$/);
        await expect(page.locator(`[data-testid="picker-workspace"][data-workspace="${workspace.id}"]`)).toHaveCount(0);
        expect((await page.request.get(`${path}/chats`)).status()).toBe(404);
        expect(await page.evaluate(() => localStorage.getItem('thesis.workspace'))).toBeNull();
        await page.reload();
        await expect(page.getByTestId('stale-workspace-notice')).toHaveCount(0);
    } finally {
        await page.request.delete(path);
    }
});

test('deletes a workspace directly from the picker', async ({ page }) => {
    await page.addInitScript(() => {
        localStorage.setItem('thesis.backend', 'local');
        localStorage.removeItem('thesis.workspace');
    });
    const created = await page.request.post('/api/v1/workspaces', { data: { name: `Picker delete ${Date.now()}` } });
    const workspace = await created.json();
    const path = `/api/v1/workspaces/${workspace.id}`;
    try {
        await page.goto('/w');
        await page.getByRole('button', { name: `Delete ${workspace.name}`, exact: true }).click();
        await page.getByTestId('workspace-delete-confirmation').fill(workspace.name);
        await page.getByTestId('workspace-delete-submit').click();
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await expect(page.locator(`[data-testid="picker-workspace"][data-workspace="${workspace.id}"]`)).toHaveCount(0);
        expect((await page.request.get(path)).status()).toBe(404);
        await expect(page.getByTestId('picker-create-workspace')).toBeVisible();
    } finally {
        await page.request.delete(path);
    }
});
