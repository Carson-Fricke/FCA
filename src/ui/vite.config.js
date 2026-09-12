import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// SvelteKit's `PUBLIC_` prefix governs `$env/*`, not Vite's `import.meta.env`.
// Expose it to the client bundle so `$lib/api` can read the API base and token.
// In dev, proxy the Thesis API same-origin so the browser never issues a
// cross-origin request (the Thesis API publishes no CORS layer yet).
const thesisApiTarget = process.env.THESIS_API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

// The SPA deliberately carries no credential (no bearer in the bundle). The
// same-origin proxy is the credential boundary: it injects the Thesis API
// bearer server-side, mirroring how the Azure Static Web App linked backend
// fronts the API in production.
const thesisApiToken = (process.env.THESIS_API__BEARER_TOKEN ?? '').trim();
const authHeaders = thesisApiToken ? { Authorization: `Bearer ${thesisApiToken}` } : {};

export default defineConfig({
	envPrefix: ['VITE_', 'PUBLIC_'],
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			// The SPA targets the same-origin `/api` prefix (the Azure Static Web
			// App linked-backend convention). In dev, proxy `/api/*` to the local
			// Thesis API and strip the prefix, so dev matches production.
			'/api': {
				target: thesisApiTarget,
				changeOrigin: true,
				headers: authHeaders,
				rewrite: (path) => path.replace(/^\/api/, '')
			},
			'/v1': { target: thesisApiTarget, changeOrigin: true, headers: authHeaders },
			'/health': { target: thesisApiTarget, changeOrigin: true, headers: authHeaders },
			'/healthz': { target: thesisApiTarget, changeOrigin: true, headers: authHeaders }
		}
	}
});
