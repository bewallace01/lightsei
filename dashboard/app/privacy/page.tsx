// Public privacy policy. Linked from the App Store Connect listing
// and from in-app surfaces that ask operators or end users to share
// personal data.
//
// Plain HTML, no auth. The App Store reviewer will visit this URL
// while the operator dashboard chrome is irrelevant. The page covers
// both operator usage (the primary audience) and end-user usage
// (the secondary, dual-identity surface).

export const metadata = {
  title: "Privacy Policy — Lightsei",
  description: "How Lightsei collects, uses, and protects your data.",
};

export default function PrivacyPolicyPage() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-12 text-gray-900">
      <h1 className="text-3xl font-semibold tracking-tight">
        Privacy Policy
      </h1>
      <p className="mt-2 text-sm text-gray-500">Effective: 2026-06-01</p>

      <p className="mt-8 leading-relaxed">
        Lightsei is a platform for building and running a team of AI
        agents for your business. This policy explains what we collect
        from operators and end users, how we use it, and what you
        control.
      </p>

      <Section title="1. What we collect">
        <p>
          <span className="font-medium">Account.</span> When you sign
          up, we collect your email address. If you sign in with Apple
          or Google, we receive only the identifiers those providers
          share with us.
        </p>
        <p>
          <span className="font-medium">Workspace data.</span> Agent
          configurations you create, project READMEs you upload, trust
          zone settings, capability allow-lists, secrets you store, and
          run logs (including agent inputs, outputs, and traces).
        </p>
        <p>
          <span className="font-medium">Integration tokens.</span> When
          you connect a third-party service (Slack, Google Workspace,
          and similar), we store the OAuth tokens needed to call that
          service on your behalf.
        </p>
        <p>
          <span className="font-medium">Customer messages.</span> When
          your agents handle a chat from one of your customers, we
          store the message content so your agents can respond and you
          can review what happened.
        </p>
        <p>
          <span className="font-medium">End-user data.</span> If you
          use Lightsei as the customer of a business that runs Lightsei
          (signed in with a magic link from your phone), we store your
          email address, your push token if you allow notifications,
          and the chats you have with each business you connect to.
        </p>
        <p>
          <span className="font-medium">Device telemetry.</span> Basic
          request metadata (timestamps, user agent, IP at request time)
          for security and debugging. We do not collect contacts,
          location, photos, microphone, or browsing history.
        </p>
      </Section>

      <Section title="2. How we use it">
        <p>
          We use your account data to sign you in, deliver the
          notifications you have opted into, and bill you for paid
          plans.
        </p>
        <p>
          We use your workspace data to run your agents, surface their
          activity on your dashboard, and enforce the trust zones and
          capability rules you have configured.
        </p>
        <p>
          We use integration tokens only to make the API calls your
          agents trigger. We never read connected accounts for any
          other purpose.
        </p>
      </Section>

      <Section title="3. Trust zones: what each assistant can see">
        <p>
          Every agent in your workspace runs inside a labeled trust
          zone. The trust zone is enforced technically: an agent
          cannot read or write information outside its zone, even if
          it appears in a message or a connected service. This is the
          core safety property of Lightsei.
        </p>
        <p>
          We do not sell your data. We do not share workspace data
          across workspaces. End-user chats with a particular business
          are visible only to that business.
        </p>
      </Section>

      <Section title="4. Service providers">
        <p>
          We rely on the following providers to operate Lightsei. They
          process data on our behalf under their own privacy policies.
        </p>
        <ul className="ml-5 list-disc">
          <li>Anthropic and OpenAI, for the language model calls that power your assistants.</li>
          <li>Apple Push Notification service, to deliver push notifications.</li>
          <li>Resend, to send sign-in and notification emails.</li>
          <li>Stripe, to process payments on paid plans.</li>
          <li>Railway and Cloudflare, to host the service.</li>
        </ul>
      </Section>

      <Section title="5. Data retention">
        <p>
          Workspace data, including run logs, persists until you
          delete the agent, workspace, or account that owns it.
        </p>
        <p>
          End-user chats remain until you remove a business from your
          account (Settings on a business chat, then Unsubscribe). The
          chat soft-deletes so it is no longer accessible to the
          business or to you.
        </p>
        <p>
          You may request full deletion of your account and all
          associated data by emailing the address below.
        </p>
      </Section>

      <Section title="6. Children's privacy">
        <p>
          Lightsei is not intended for children under 13. If we learn
          that we have collected information from a child under 13, we
          will delete it.
        </p>
      </Section>

      <Section title="7. Your rights">
        <ul className="ml-5 list-disc">
          <li>
            Access: contact us to request a copy of your data.
          </li>
          <li>
            Deletion: contact us to delete your account and all
            associated data.
          </li>
          <li>
            Notifications: turn off push notifications in your device
            Settings, or per surface in the app.
          </li>
        </ul>
      </Section>

      <Section title="8. Changes">
        <p>
          We may update this policy. Material changes will be
          communicated by email or by in-app notice.
        </p>
      </Section>

      <Section title="9. Contact">
        <p>
          Questions, requests, or concerns:{" "}
          <a
            href="mailto:wallacebailey32@gmail.com"
            className="text-indigo-700 underline"
          >
            wallacebailey32@gmail.com
          </a>
        </p>
      </Section>
    </main>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-10 space-y-3 leading-relaxed">
      <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}
