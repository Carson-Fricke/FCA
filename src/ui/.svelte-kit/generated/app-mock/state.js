// Test-only mock of the SvelteKit runtime `$app/state` module so vitest can
// import route components that read `page.params` without a full SvelteKit
// build. Tests mutate `page.params` before rendering (the mock is inert and
// non-reactive; render-time reads see the current values).
export const page = {
	params: { workspace: '', id: '' },
	url: new URL('http://localhost/'),
	route: { id: null },
	status: 200,
	error: null,
	data: {},
	state: {},
	form: null
};
