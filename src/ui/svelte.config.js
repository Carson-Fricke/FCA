import adapter from '@sveltejs/adapter-static';

// The SPA is built with adapter-static and deployed to Azure Static Web Apps.
// The build embeds no API credential: the browser targets the same-origin
// `/api` prefix, which the Standard-tier linked backend proxies to the
// internal Thesis Container App (see docs/adr/adr-001-invite-only-*.md).
/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		adapter: adapter({ fallback: 'index.html' })
	}
};

export default config;
