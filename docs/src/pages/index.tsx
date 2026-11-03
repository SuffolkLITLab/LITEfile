import React from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';
import styles from './styles.module.css';

export default function Home(): React.JSX.Element {
  const { siteConfig } = useDocusaurusContext();

  return (
    <Layout
      title="Documentation"
      description="LITEfile: Streamlined, accessible electronic court filing and platform documentation"
    >
      <main>
        {/* Hero Section */}
        <header className={styles.hero}>
          <div className={styles.heroInner}>
            <div className={styles.heroText}>
              <h1>
                Streamlined <strong>Court E-Filing</strong> Made Accessible
              </h1>
              <p className={styles.heroSubtitle}>
                LITEfile is a modern, lightweight web application designed to simplify electronic court filing for self-represented litigants, legal aid advocates, and court partners through plain-language guidance, document checklists, and automated field extraction.
              </p>
              <div className={styles.heroCTAButtons}>
                <Link
                  className="button button--primary button--lg"
                  to="/docs/user-guide/overview"
                >
                  End-user guide
                </Link>
                <Link
                  className="button button--secondary button--lg"
                  to="/docs/partners-courts"
                >
                  Partner & court guide <span className="wip-badge">WIP</span>
                </Link>
                <Link
                  className="button button--outline button--primary button--lg"
                  to="/docs/admin"
                >
                  Admin & deployment
                </Link>
              </div>
            </div>
            <div className={styles.heroImageContainer}>
              <img
                src={useBaseUrl('/img/undraw_sync-files_64mj.svg')}
                alt="Electronic court filing illustration"
                className={styles.heroImage}
              />
            </div>
          </div>
        </header>

        {/* Vision Quote Section */}
        <section className={styles.indexSection}>
          <blockquote className={styles.pullQuote}>
            <p>
              "E-filing shouldn't require a law degree or an encyclopedic knowledge of court codes. LITEfile turns complex filing requirements into clear, step-by-step guidance."
            </p>
            <cite>
              — <strong>Suffolk University Law School LIT Lab</strong> & Partner Courts
            </cite>
          </blockquote>
        </section>

        {/* Feature Grid */}
        <section className={styles.indexSection}>
          <div className={styles.sectionHeader}>
            <h2>Core capabilities & design</h2>
            <p>
              Built from the ground up to reduce rejected filings, eliminate clerk confusion, and streamline access to justice.
            </p>
          </div>

          <div className={styles.cardGrid}>
            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>📋</div>
              <h3>Guided filing workflow</h3>
              <p>
                A filing journey that adapts to new and existing cases, guiding users through document upload, case confirmation, people, fees, review, and submission.
              </p>
              <Link to="/docs/user-guide/walkthrough" className="button button--link">
                File step by step →
              </Link>
            </div>

            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>🤖</div>
              <h3>AI document extraction</h3>
              <p>
                Optional AI reading of PDF or Word documents suggests case details for filers to check. Filers can opt out and enter the details manually.
              </p>
              <Link to="/docs/partners-courts/ai-customization" className="button button--link">
                Customize AI prompts →
              </Link>
            </div>

            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>🏛️</div>
              <h3>Document checklists & plans</h3>
              <p>
                State and court guidance groups documents as commonly included, often included, or may apply. Checklists can depend on the filer’s side and remain optional guidance.
              </p>
              <Link to="/docs/partners-courts/document-checklists" className="button button--link">
                Checklist configuration →
              </Link>
            </div>

            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>⚙️</div>
              <h3>Jurisdiction YAML configs</h3>
              <p>
                YAML settings let partners configure case guidance, waiver help, court logos, contacts, and supported wording. Other field labels and behaviors may require code changes.
              </p>
              <Link to="/docs/partners-courts/jurisdiction-config" className="button button--link">
                Jurisdiction specs →
              </Link>
            </div>

            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>🚀</div>
              <h3>Modern cloud deployment</h3>
              <p>
                Containerized Python 3.12 / Django ASGI stack with uv package management, Gunicorn/Uvicorn, WhiteNoise static delivery, and AWS S3 secure document handling.
              </p>
              <Link to="/docs/admin/deployment" className="button button--link">
                Deployment guide →
              </Link>
            </div>

            <div className={styles.featureCard}>
              <div className={styles.featureIcon}>🔗</div>
              <h3>EFSP & AssemblyLine ready</h3>
              <p>
                Integrates with the Tyler Technologies EFM / EFSP REST API and pairs seamlessly with Document Assembly Line / Docassemble guided interviews.
              </p>
              <Link to="/docs/partners-courts/interview-integration" className="button button--link">
                Integration guide →
              </Link>
            </div>
          </div>
        </section>

        {/* Partners Section */}
        <section className={styles.partnerSection}>
          <div className={styles.indexSection} style={{ margin: '0 auto' }}>
            <div className={styles.sectionHeader} style={{ marginBottom: '1.5rem' }}>
              <h2>Partners & collaborators</h2>
              <p>
                Developed with support from court systems, legal aid organizations, and academic legal tech innovators.
              </p>
            </div>
            <div className={styles.partnerLogos}>
              <div className={styles.partnerLogoItem}>
                <img src={useBaseUrl('/img/court-logo-illinois.svg')} alt="Illinois Courts" />
                <span className={styles.partnerLogoLabel}>Illinois Courts</span>
              </div>
              <div className={styles.partnerLogoItem}>
                <img src={useBaseUrl('/img/suffolk-lit-lab-logo.svg')} alt="Suffolk LIT Lab" />
                <span className={styles.partnerLogoLabel}>Suffolk LIT Lab</span>
              </div>
              <div className={styles.partnerLogoItem}>
                <img src={useBaseUrl('/img/logo-ilao.png')} alt="Illinois Legal Aid Online" />
                <span className={styles.partnerLogoLabel}>Illinois Legal Aid Online</span>
              </div>
              <div className={styles.partnerLogoItem}>
                <img src={useBaseUrl('/img/logo-lsc.svg')} alt="Legal Services Corporation" />
                <span className={styles.partnerLogoLabel}>Legal Services Corporation</span>
              </div>
              <div className={styles.partnerLogoItem}>
                <img src={useBaseUrl('/img/logo-sji.png')} alt="State Justice Institute" />
                <span className={styles.partnerLogoLabel}>State Justice Institute</span>
              </div>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
