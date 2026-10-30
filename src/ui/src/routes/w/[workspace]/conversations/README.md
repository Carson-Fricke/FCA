# Fullscreen conversations

This route presents conversation selection and the shared chat surface on a
continuous canvas. `ChatHostController` owns listing, selection, drafts, sends
and deletion; `ChatSurface` owns transcript, scope and outline presentation.
The page does not create another message cache, stream or delivery path.

- `+page.svelte`: Composes a collapsible conversation list and the shared chat
  surface, maps scope navigation to the URL, and presents consent/deletion
  dialogs. Narrow layouts use a dismissible picker drawer. Closing selection
  leaves the active chat and draft mounted.
