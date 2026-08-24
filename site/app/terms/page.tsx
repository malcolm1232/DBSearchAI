import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Terms of Service - DBSearch.AI",
  description:
    "The terms that govern use of the DBSearch.AI website and its live demo. Governed by Singapore law.",
};

const LAST_UPDATED = "5 July 2026";

type Section = { heading: string; paragraphs: React.ReactNode[] };

const SECTIONS: Section[] = [
  {
    heading: "1. Agreement",
    paragraphs: [
      <>
        These terms govern your use of this website and its interactive demo,
        operated under the name <strong>DBSearch.AI</strong>{" "}
        (&ldquo;we&rdquo;, &ldquo;us&rdquo;), based in Singapore. By using the
        site you accept these terms. If you do not accept them, do not use the
        site.
      </>,
      <>
        These terms cover the <strong>website and demo only</strong>.
        Deployments of the DBSearch.AI product are governed separately: the
        open-source edition by the license in its code repository, and any
        managed engagement by the agreement signed for it.
      </>,
    ],
  },
  {
    heading: "2. Acceptable use",
    paragraphs: [
      <>
        You may use the site and demo for evaluating DBSearch.AI. You agree
        not to misuse them - including attempting to breach security or access
        controls, probing or load-testing the demo, scraping at volume,
        submitting unlawful content, or entering confidential or personal
        information into the demo (it is a shared demonstration environment
        seeded with fictional documents).
      </>,
    ],
  },
  {
    heading: "3. The demo is illustrative",
    paragraphs: [
      <>
        The live demo runs against fictional sample data in an isolated
        environment. It exists to illustrate how the product behaves and makes
        no promise about performance, accuracy, or availability. Demo answers
        are generated from sample documents and are not advice of any kind.
      </>,
    ],
  },
  {
    heading: "4. Intellectual property",
    paragraphs: [
      <>
        The content of this website - text, design, and imagery - belongs to
        us. The DBSearch.AI name and marks may not be used to imply
        endorsement without written permission. The product&rsquo;s source
        code is licensed separately under the license published in its
        repository; nothing in these terms limits the rights that license
        grants you.
      </>,
    ],
  },
  {
    heading: "5. No warranty",
    paragraphs: [
      <>
        The site and demo are provided &ldquo;as is&rdquo; and &ldquo;as
        available&rdquo;, without warranties of any kind, express or implied.
        Statements on this site describe the product&rsquo;s design and
        intended behaviour and are not a contractual specification unless
        incorporated into a signed agreement.
      </>,
    ],
  },
  {
    heading: "6. Limitation of liability",
    paragraphs: [
      <>
        To the maximum extent permitted by law, we are not liable for any
        indirect, incidental, special, or consequential loss arising from use
        of this website or demo. Our total liability for any claim relating to
        the site is limited to S$100. Nothing in these terms excludes
        liability that cannot be excluded under applicable law.
      </>,
    ],
  },
  {
    heading: "7. Changes",
    paragraphs: [
      <>
        We may update these terms from time to time; the current version is
        always the one published here, with its date above. Continuing to use
        the site after a change means you accept the updated terms.
      </>,
    ],
  },
  {
    heading: "8. Governing law & contact",
    paragraphs: [
      <>
        These terms are governed by the laws of Singapore, and the courts of
        Singapore have exclusive jurisdiction over any dispute relating to
        them. Questions:{" "}
        <a className="text-accent underline-offset-4 hover:underline" href="mailto:privacy@dbsearch.ai">
          privacy@dbsearch.ai
        </a>
        .
      </>,
    ],
  },
];

export default function TermsPage() {
  return (
    <main>
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 lg:py-24">
          <p className="text-micro font-mono text-fg-muted">
            Legal
          </p>
          <h1 className="mt-4 text-4xl font-semibold tracking-tight text-fg">
            Terms of Service
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
