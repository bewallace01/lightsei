// Public support page. Linked from the App Store Connect listing.
// Apple's reviewers will visit this URL to confirm that end users
// have a way to reach a real person if something goes wrong.

export const metadata = {
  title: "Support — Lightsei",
  description: "How to reach Lightsei for help with your account or chats.",
};

export default function SupportPage() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-12 text-gray-900">
      <h1 className="text-3xl font-semibold tracking-tight">Support</h1>

      <p className="mt-6 leading-relaxed">
        Need help with your Lightsei account or a chat with a brand?
        We will get back to you within two business days.
      </p>

      <section className="mt-10">
        <h2 className="text-xl font-semibold tracking-tight">Contact</h2>
        <p className="mt-3 leading-relaxed">
          Email{" "}
          <a
            href="mailto:wallacebailey32@gmail.com"
            className="text-indigo-700 underline"
          >
            wallacebailey32@gmail.com
          </a>{" "}
          and include:
        </p>
        <ul className="ml-5 mt-2 list-disc space-y-1 leading-relaxed">
          <li>The email address you signed in with.</li>
          <li>
            The name of the brand whose chat you are asking about, if
            applicable.
          </li>
          <li>A description of what happened and what you expected.</li>
        </ul>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold tracking-tight">Common asks</h2>
        <div className="mt-3 space-y-5 leading-relaxed">
          <div>
            <p className="font-medium">
              I cannot sign in / I am not getting the magic link email.
            </p>
            <p className="mt-1 text-gray-700">
              Check your spam folder. If the email still has not
              arrived after a few minutes, write to us with the email
              address you tried.
            </p>
          </div>
          <div>
            <p className="font-medium">
              I want to remove a brand from my account.
            </p>
            <p className="mt-1 text-gray-700">
              Open the brand chat, tap Settings, then Unsubscribe. The
              chat soft-deletes and the brand can no longer reach you.
            </p>
          </div>
          <div>
            <p className="font-medium">
              I want to delete my Lightsei account entirely.
            </p>
            <p className="mt-1 text-gray-700">
              Email us with the address you signed in with and we will
              delete your account and all associated data.
            </p>
          </div>
          <div>
            <p className="font-medium">
              I am not receiving notifications.
            </p>
            <p className="mt-1 text-gray-700">
              Check iOS Settings, then Lightsei, then Notifications.
              Notifications must be allowed for the app to deliver
              alerts. Also check the notification preference on each
              brand chat (Settings on the chat).
            </p>
          </div>
        </div>
      </section>

      <section className="mt-10">
        <p className="text-sm text-gray-500">
          For our privacy policy, see{" "}
          <a href="/privacy" className="text-indigo-700 underline">
            /privacy
          </a>
          .
        </p>
      </section>
    </main>
  );
}
