import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [svelte({ hot: !process.env.VITEST })],
	resolve: {
		conditions: process.env.VITEST ? ['browser'] : undefined,
		alias: {
			'$app/navigation': fileURLToPath(new URL('./src/tests/app-navigation.ts', import.meta.url)),
			'$app/state': fileURLToPath(new URL('./src/tests/app-state.ts', import.meta.url)),
			$lib: fileURLToPath(new URL('./src/lib', import.meta.url)),
			$app: fileURLToPath(new URL('./.svelte-kit/generated/app-mock', import.meta.url))
		}
	},
	test: {
		environment: 'jsdom',
		setupFiles: ['src/tests/setup.ts'],
		include: ['src/tests/**/*.test.ts']
	}
});
