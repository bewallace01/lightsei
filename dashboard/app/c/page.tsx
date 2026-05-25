// Phase 25.7: end-user consumer-chat surface placeholder.
//
// `/c` is the route the Phase 25.2 magic-link consume side redirects
// signed-in end users to. The real UI (vendor list, conversation
// list, thread view, PWA install prompt) ships in Phase 26.
//
// Today it's a static "you made it; the real chat lands in Phase 26"
// stub. Reserving the route slot now means the next-build sweep
// covers it and the operator dashboard nav doesn't accidentally
// shadow it later.

export const metadata = {
  title: "Lightsei chat",
};

export default function ConsumerChatPlaceholderPage() {
  return (
    <main className="min-h-screen px-8 py-16 max-w-xl mx-auto text-center">
      <h1 className="text-3xl font-semibold tracking-tight mb-3">
        You&apos;re signed in
      </h1>
      <p className="text-sm text-gray-500 mb-8">
        The real Lightsei chat surface arrives in Phase 26. For now
        your end-user account exists and the widget-on-vendor-sites
        flow picks up your identity automatically.
      </p>
      <p className="text-xs text-gray-400">
        End-user identity from Phase 25.
      </p>
    </main>
  );
}
