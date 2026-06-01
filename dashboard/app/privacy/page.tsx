// Public privacy policy. Linked from the App Store Connect listing
// and from in-app surfaces that ask end users to share personal data.
//
// Plain HTML, no auth. The App Store reviewer will visit this URL
// while the operator dashboard chrome is irrelevant — this page is
// meant for end users + reviewers, not the operator.

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
        Lightsei is a chat platform that connects end users with the
        brands they choose to do business with. This policy explains
        what we collect, why, and what you control.
      </p>

      <Section title="1. What we collect">
        <p>
          When you sign in, we collect your email address. You may also
          choose Sign in with Apple, in which case we receive only the
          identifiers Apple shares.
        </p>
        <p>
          When you chat with a brand, we store the messages you send and
          receive so the brand can serve you and you can see your
          history.
        </p>
        <p>
          When you allow notifications, we store a device push token
          so we can deliver notifications to your device.
        </p>
        <p>
          We do not collect contacts, location, photos, microphone, or
          browsing history.
        </p>
      </Section>

      <Section title="2. How we use it">
        <p>
          We use your email to sign you in and to send notifications
          you have opted into.
        </p>
        <p>
          We use chat content to deliver messages between you and the
          brand you are chatting with. Each brand only sees the chats
          they have with you.
        </p>
      </Section>

      <Section title="3. Trust zones: who sees what">
        <p>
          Every brand on Lightsei has a labeled trust zone shown at the
          top of every chat. The trust zone determines what kinds of
          information the brand can see. We enforce this technically:
          a brand cannot access information outside their zone, even
          if it appears in a message.
        </p>
        <p>
          We do not sell your data. We do not share your data with
          brands beyond the chats you have with them.
        </p>
      </Section>

      <Section title="4. Service providers">
        <p>
          We rely on the following providers to operate Lightsei. They
          process data on our behalf under their own privacy policies.
        </p>
        <ul className="ml-5 list-disc">
          <li>Apple Push Notification service, to deliver notifications.</li>
          <li>Resend, to send sign-in emails.</li>
          <li>Railway and Cloudflare, to host the service.</li>
        </ul>
      </Section>

      <Section title="5. Data retention">
        <p>
          Your chats remain available until you remove a brand from
          your account (Settings on a brand chat, then Unsubscribe).
          Removing a brand soft-deletes the chat so it is no longer
          accessible to the brand or to you.
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
            Settings, or per brand in the app.
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
