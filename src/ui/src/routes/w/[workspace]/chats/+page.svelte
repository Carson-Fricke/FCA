<script lang="ts">
	import { goto, replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
	import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
	import CleoPlanPanel from '$lib/components/chat/CleoPlanPanel.svelte';
	import WorkflowTimeline from '$lib/components/chat/WorkflowTimeline.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import {
		confirmConsent,
		getConsentProposal,
		getExecution,
		getWorkspaceState,
		listChats,
		listMessages,
		listStrategies,
		postMessage,
		replayExecutionEvents
	} from '$lib/api';
	import { fixtureMode } from '$lib/api';
	import { ExecutionEventStream, LocalCursorStore, executionEventOf } from '$lib/sse';
	import { notify } from '$lib/workspace.svelte';
	import type {
		ChatMessage,
		ChatThread,
		ConsentProposal,
		EvidenceItem,
		ExecutionEvent,
		Strategy,
		SteeringPercolationTarget,
		WorkflowExecutionRecord,
		WorkspaceStateData
	} from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let messages = $state<ChatMessage[]>([]);
	let execution = $state<WorkflowExecutionRecord | null>(null);
	let events = $state<ExecutionEvent[]>([]);
	let percolations = $state<Record<string, SteeringPercolationTarget[]>>({});
	let proposal = $state<ConsentProposal | null>(null);
	let consentBusy = $state(false);
	let stateData = $state<WorkspaceStateData | null>(null);
	let strategies = $state<Strategy[]>([]);
	let busy = $state(false);
	let cursorStore = new LocalCursorStore();
	let stream: ExecutionEventStream | null = null;

	const activeChat = $derived(chats.find((c) => c.chat_id === chatId) ?? null);
	const activeExecutionId = $derived(execution?.execution_id ?? null);
	const cleoPlan = $derived(stateData?.cleo ?? null);
	const evidence = $derived(stateData?.evidence ?? []);

	$effect(() => {
		let cancelled = false;
		listChats(workspaceId).then((items) => {
			if (cancelled) return;
			chats = items;
			if (!chatId) {
				const requested = page.url.searchParams.get('chat');
				chatId = requested && items.some((c) => c.chat_id === requested)
					? requested
					: (items[0]?.chat_id ?? null);
			}
		});
		listStrategies(workspaceId).then((items) => {
			if (!cancelled) strategies = items;
		});
		getWorkspaceState(workspaceId).then((ws) => {
			if (!cancelled) stateData = ws;
		});
		getConsentProposal(workspaceId).then((p) => {
			if (!cancelled) proposal = p;
		});
		return () => {
			cancelled = true;
		};
	});

	// Load the selected chat's snapshot: messages, execution, and the durable
	// event replay from the persisted cursor.
	$effect(() => {
		const id = chatId;
		if (!id) {
			messages = [];
			execution = null;
			events = [];
			return;
		}
		let cancelled = false;
		const requested = page.url.searchParams.get('chat');
		if (requested !== id) {
			replaceState(`/w/${workspaceId}/chats?chat=${id}`, {});
		}
		listMessages(workspaceId, id).then((items) => {
			if (!cancelled) messages = items;
		});
		getExecution(workspaceId, id).then((exec) => {
			if (cancelled) return;
			execution = exec;
			if (exec) {
				replayExecutionEvents(workspaceId, exec.execution_id, 0).then((replay) => {
					if (!cancelled) events = replay;
				});
				startStream(exec.execution_id);
			} else {
				stopStream();
			}
		});
		return () => {
			cancelled = true;
		};
	});

	function startStream(executionId: string): void {
		stopStream();
		if (fixtureMode) {
			// Fixtures feed the initial snapshot; the SSE client itself is real
			// code and runs against the API in real-fetch mode.
			return;
		}
		stream = new ExecutionEventStream({
			baseUrl: '',
			workspaceId,
			executionId,
			cursorStore
		});
		void stream.start((frame) => {
			const event = executionEventOf(frame);
			if (event && !events.some((e) => e.eventId === event.eventId)) {
				events = [...events, event];
			}
		});
	}

	function stopStream(): void {
		stream?.stop();
		stream = null;
	}

	async function send(input: {
		body: string;
		attachmentIds: string[];
		steeringIntent: string | null;
	}): Promise<void> {
		if (!chatId) return;
		busy = true;
		try {
			const result = await postMessage(workspaceId, chatId, input);
			messages = [...messages, result.message];
			if (result.percolated.length) {
				percolations = { ...percolations, [result.message.message_id]: result.percolated };
			}
			if (input.steeringIntent && activeExecutionId) {
				notify('Steering acknowledged — durable percolation recorded');
			}
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Message failed', true);
		} finally {
			busy = false;
		}
	}

	async function confirmProposal(confirmation: {
		bundleHash: string;
		environment: string;
		action: string;
		confirmationToken: string;
	}): Promise<void> {
		consentBusy = true;
		try {
			await confirmConsent(workspaceId, confirmation);
			proposal = null;
			notify('Structured consent recorded — grant issued');
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Confirmation failed', true);
		} finally {
			consentBusy = false;
		}
	}

	/** Workspace evidence deep-links mentioned in a message body. Unused in
	 * the transcript since evidence scoping moved to sandboxed subagent
	 * views; kept for the workspace-state projection. */
	function evidenceFor(messageId: string): EvidenceItem[] {
		const message = messages.find((m) => m.message_id === messageId);
		if (!message) return [];
		return evidence.filter((item) => {
			if (item.kind === 'backtest') {
				return /bt-\d+/.test(message.body) && message.body.includes(item.title.match(/bt-\d+/)?.[0] ?? '∅');
			}
			return message.body.includes(item.reference) || message.body.includes(item.title);
		});
	}

	$effect(() => {
		return () => stopStream();
	});
</script>

<svelte:head><title>AI chats — Thesis</title></svelte:head>

<div class="flex h-full min-h-0 gap-4">
	<section class="flex w-72 shrink-0 flex-col rounded-xl border border-white/5 bg-surface-850/60">
		<div class="flex items-center gap-2 border-b border-white/5 px-4 py-3">
			<h2 class="text-sm font-semibold text-slate-200">Conversations</h2>
			<span class="rounded-full bg-surface-800 px-1.5 font-mono text-[10px] text-slate-400">
				{chats.length}
			</span>
		</div>
		<ul class="min-h-0 flex-1 space-y-1 overflow-y-auto p-2" data-testid="conversation-list">
			{#each chats as chat (chat.chat_id)}
				<li>
					<button
						type="button"
						class="w-full rounded-lg px-3 py-2 text-left transition
							{chat.chat_id === chatId
							? 'border border-accent/40 bg-accent/10'
							: 'border border-transparent hover:bg-surface-800/60'}"
						onclick={() => (chatId = chat.chat_id)}
						data-testid="conversation-item"
						data-chat={chat.chat_id}
					>
						<p class="truncate text-sm {chat.chat_id === chatId ? 'text-accent' : 'text-slate-300'}">
							{chat.title}
						</p>
						<p class="text-[11px] text-slate-500">
							{chat.kind === 'investigation' ? 'investigation' : 'chat'} · {chat.updated_at?.slice(0, 10) ?? chat.created_at.slice(0, 10)}
						</p>
					</button>
				</li>
			{:else}
				<li class="px-3 py-6 text-center text-xs text-slate-600">
					Your next good idea starts with a conversation.
				</li>
			{/each}
		</ul>
		<div class="border-t border-white/5 px-4 py-2 font-mono text-[10px] text-slate-600">
			Scoped to workspace {workspaceId}
		</div>
	</section>

	<section class="flex min-w-0 flex-1 flex-col rounded-xl border border-white/5 bg-surface-900/50">
		{#if activeChat}
			<header class="flex items-center gap-2 border-b border-white/5 px-4 py-3">
				<h1 class="truncate text-sm font-semibold text-slate-100">{activeChat.title}</h1>
				{#if activeChat.kind === 'investigation'}
					<span class="rounded-full border border-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent">
						investigation
					</span>
				{/if}
				{#if activeExecutionId}
					<span class="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-warn">
						<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
						execution {activeExecutionId} · {execution?.state} · phase {execution?.phase ?? '—'}
					</span>
				{/if}
			</header>

			<div class="min-h-0 flex-1 overflow-y-auto p-4" data-testid="transcript-scroll">
				<ChatTranscript
					{messages}
					{percolations}
					{proposal}
					onConfirmConsent={confirmProposal}
					onRejectConsent={() => {
						proposal = null;
						notify('Consent proposal rejected');
					}}
					{consentBusy}
				/>
			</div>

			<div class="border-t border-white/5 p-3">
				<WorkflowTimeline events={events} />
			</div>
		{:else}
			<div class="flex flex-1 items-center justify-center p-6">
				<EmptyState
					title="No conversation selected"
					hint="Start a chat from the copilot or ask the agent to draft a strategy."
				/>
			</div>
		{/if}

		<ChatComposer
			onSubmit={send}
			{activeExecutionId}
			{busy}
		/>
	</section>

	<aside class="hidden w-80 shrink-0 overflow-y-auto rounded-xl border border-white/5 bg-surface-850/60 p-3 xl:block">
		<h2 class="mb-2 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
			CLEO projection · read-only
		</h2>
		{#if cleoPlan}
			<CleoPlanPanel plan={cleoPlan} timeline={stateData?.taskTimeline ?? []} />
		{:else}
			<EmptyState title="No CLEO plan projected" hint="Plans appear when a workflow drives this workspace." />
		{/if}
	</aside>
</div>
