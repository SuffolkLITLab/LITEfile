// @ts-check

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  userGuide: [
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: [
        'user-guide/overview',
        'user-guide/before-you-begin',
        'user-guide/can-i-file-here',
        'user-guide/get-help',
      ],
    },
    {
      type: 'category',
      label: 'File step by step',
      collapsed: false,
      link: { type: 'doc', id: 'user-guide/walkthrough' },
      items: [
        'user-guide/prepare-documents',
        'user-guide/create-account',
        'user-guide/sign-in',
        'user-guide/start-filing',
        'user-guide/upload-documents',
        'user-guide/case-information',
        'user-guide/organize-documents',
        'user-guide/people',
        'user-guide/fees',
        'user-guide/review-submit',
        'user-guide/check-status',
      ],
    },
    {
      type: 'category',
      label: 'After you file',
      collapsed: false,
      items: [
        'user-guide/after-you-file',
        'user-guide/fix-returned-filing',
        'user-guide/serve-papers',
      ],
    },
    {
      type: 'category',
      label: 'More help',
      collapsed: false,
      items: [
        'user-guide/save-and-return',
        'user-guide/words-to-know',
        'user-guide/faq',
      ],
    },
  ],
  partnersCourts: [
    {
      type: 'category',
      label: 'Partner & court guide (WIP)',
      collapsed: false,
      items: [
        'partners-courts/index',
        'partners-courts/customization',
        'partners-courts/jurisdiction-config',
        'partners-courts/email-review',
        'partners-courts/maintained-data',
        'partners-courts/filing-availability',
        'partners-courts/document-checklists',
        'partners-courts/document-finder',
        'partners-courts/ai-customization',
        'partners-courts/interview-integration',
        'partners-courts/reporting',
        'partners-courts/maintenance',
      ],
    },
  ],
  adminGuide: [
    {
      type: 'category',
      label: 'Admin & deployment (WIP)',
      collapsed: false,
      items: [
        'admin/index',
        'admin/architecture',
        'admin/configuration',
        'admin/deployment',
        'admin/filing-code-operations',
        'admin/staff-onboarding',
        'admin/service-maintenance',
        'admin/documentation-review',
        'admin/development',
      ],
    },
  ],
};

export default sidebars;
