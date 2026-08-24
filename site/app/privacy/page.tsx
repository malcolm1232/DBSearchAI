import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy - DBSearch.AI",
  description:
    "What the DBSearch.AI website collects (a demo-request form and live-demo queries), what it never collects, and your rights under the PDPA.",
};

const LAST_UPDATED = "5 July 2026";

type Section = { heading: string; paragraphs: React.ReactNode[] };

const SECTIONS: Section[] = [
  {
    heading: "1. Who we are",
    paragraphs: [
      <>
        This website and its live demo are operated under the name{" "}
        <strong>DBSearch.AI</strong> (&ldquo;we&rdquo;, &ldquo;us&rdquo;),
        based in Singapore. For anything in this policy, contact{" "}
        <a className="text-accent underline-offset-4 hover:underline" href="mailto:privacy@dbsearch.ai">
          privacy@dbsearch.ai
        </a>
        .
      </>,
      <>
        This policy covers <strong>this website only</strong>. The DBSearch.AI
        product is self-hosted or runs inside your own cloud tenant: your
        documents, queries, and permissions are processed on your
        infrastructure and are never sent to us. If you deploy the product,
        your organisation - not this policy - governs that data.
      </>,
    ],
  },
  {
    heading: "2. What we collect",
    paragraphs: [
      <>
        <strong>Demo-request form.</strong> If you submit the form on the demo
        page we receive the name, work email, company, and message you typed.
        We use it to respond to your request and for nothing else. Delivery is
        handled by a form-processing provider acting as our processor.
      </>,
      <>
        <strong>Live-demo queries.</strong> Questions you type into the
        interactive demo are sent to an isolated demonstration environment
        seeded with fictional sample documents, so the demo can answer them.
        They are not linked to your identity and are not used to build
        profiles. Please do not enter confidential or personal information
        into the demo.
      </>,
      <>
        <strong>Server logs.</strong> Like almost every website, our hosting
        infrastructure records basic technical logs (IP address, user agent,
        pages requested) for security and reliability. These are kept only as
        long as normal log rotation requires.
      </>,
      <>
        <strong>What we don&rsquo;t do:</strong> no advertising trackers, no
        third-party analytics scripts, no tracking cookies, and no sale or
        rental of personal data to anyone.
      </>,
    ],
  },
  {
    heading: "3. Why we may process it (legal basis)",
    paragraphs: [
      <>
        We process form submissions with your consent and to take steps you
        ask of us (responding to a demo request). We process technical logs in
        our legitimate interest of keeping the site secure and available.
      </>,
    ],
  },
  {
    heading: "4. Sharing",
    paragraphs: [
      <>
        Personal data is shared only with the service providers that run this
        site for us - web hosting and form delivery - under their capacity as
        data processors, and where the law requires disclosure. We do not sell
        personal data.
      </>,
    ],
  },
  {
    heading: "5. Retention",
    paragraphs: [
      <>
        Demo-request details are kept for as long as needed to handle your
        enquiry and any follow-up you ask for, then deleted. Technical logs
        follow the hosting provider&rsquo;s standard short rotation windows.
      </>,
    ],
  },
  {
    heading: "6. Your rights",
    paragraphs: [
      <>
        Under Singapore&rsquo;s Personal Data Protection Act (PDPA) - and, if
        it applies to you, the GDPR - you may request access to or correction
        of your personal data, and withdraw consent for us to keep it. Email{" "}
        <a className="text-accent underline-offset-4 hover:underline" href="mailto:privacy@dbsearch.ai">
          privacy@dbsearch.ai
        </a>{" "}
        and we will respond within a reasonable time.
      </>,
    ],
  },
  {
    heading: "7. Changes",
    paragraphs: [
      <>
        If this policy changes, the new version will be posted here with an
        updated date. Material changes will be flagged on this page.
      </>,
    ],
  },
];

export default function PrivacyPage() {
  return (
    <main>
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 lg:py-24">
          <p className="text-micro font-mono text-fg-muted">
            Legal
          </p>
          <h1 className="mt-4 text-4xl font-semibold tracking-tight text-fg">
            Privacy Policy
          </h1>
          <p className="mt-3 text-sm text-fg-muted">
            Last updated {LAST_UPDATED}
          </p>
        </div>
      </section>

      <section className="mx-auto max-w-3xl px-6 py-16">
        <div className="flex flex-col gap-10">
          {SECTIONS.map((section) => (
            <div key={section.heading}>
              <h2 className="text-lg font-semibold text-fg">
                {section.heading}
              </h2>
              {section.paragraphs.map((para, i) => (
                <p key={i} className="mt-3 text-sm leading-relaxed text-fg-muted">
                  {para}
                </p>
              ))}
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
