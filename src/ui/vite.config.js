import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// SvelteKit's `PUBLIC_` prefix governs `$env/*`, not Vite's `import.meta.env`.
// Expose it to the client bundle so `$lib/api` can read the API base and token.
// In dev, proxy the Thesis API same-origin so the browser never issues a
// cross-origin request (the Thesis API publishes no CORS layer yet).
const thesisApiTarget = process.env.THESIS_API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

export default defineConfig({
	envPrefix: ['VITE_', 'PUBLIC_'],
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			'/v1': { target: thesisApiTarget, changeOrigin: true },
			'/health': { target: thesisApiTarget, changeOrigin: true },
			'/healthz': { target: thesisApiTarget, changeOrigin: true }
		}
	}
});
