const {
    test,
    expect
} = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const fs = require('fs');

const findings = [];
const blockingImpacts = new Set(['critical', 'serious']);

// These are stable URLs, not workflow transitions: a change to the order of
// the filing flow should not require changing accessibility coverage.
const publicScreens = [
    ['choose jurisdiction', '/choose-jurisdiction'],
    ['about', '/about/'],
    ['terms of service', '/terms-of-service/'],
    ['sign in', '/jurisdiction/illinois/login/'],
    ['register', '/jurisdiction/illinois/register/'],
    ['password reset', '/jurisdiction/illinois/password_reset/'],
    ['find documents', '/jurisdiction/illinois/find-documents/'],
    ['find documents with no match', '/jurisdiction/illinois/find-documents/?q=xyzzy'],
    ['find documents guide', '/jurisdiction/illinois/find-documents/respond_to_an_eviction/'],
    ['find documents without a guide', '/jurisdiction/vermont/find-documents/'],
];
const signedInScreens = [
    ['filing options', '/jurisdiction/illinois/options/'],
    ['choose filing path', '/jurisdiction/illinois/filing-path/'],
    ['upload documents', '/jurisdiction/illinois/upload-documents/'],
    ['confirm filing', '/jurisdiction/illinois/extraction-review/'],
    ['check documents', '/jurisdiction/illinois/document-checklist/'],
    ['organize documents', '/jurisdiction/illinois/organize-documents/'],
    // Complete contact details skip to People; explicitly open the editable form.
    ['your information', '/jurisdiction/illinois/your-information/?edit=1'],
    ['people', '/jurisdiction/illinois/parties/'],
    ['payment', '/jurisdiction/illinois/payment/'],
    ['review', '/jurisdiction/illinois/review/'],
    ['my drafts', '/jurisdiction/illinois/my-drafts/'],
];

async function audit(page, label, scope) {
    await page.waitForLoadState('networkidle');
    const results = await new AxeBuilder({
            page
        })
        .withTags(['wcag2a', 'wcag2aa'])
        .include(scope || 'body')
        .analyze();
    for (const violation of results.violations) {
        findings.push({
            label,
            impact: violation.impact,
            rule: violation.id,
            help: violation.help,
            helpUrl: violation.helpUrl,
            targets: violation.nodes.map(node => node.target),
        });
    }
}

// The workflow screens rewrite their own URL to name the draft they are on
// (see static/js/draft-scope.js), so match the path and ignore any query or
// fragment. This still catches a screen that redirects somewhere else.
function routePattern(url) {
    url = url.split(/[?#]/)[0];
    return new RegExp(`${url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/?(?:[?#].*)?$`);
}

for (const [label, url] of publicScreens) {
    test(`accessible: ${label}`, async ({
        page
    }) => {
        await page.goto(url, {
            waitUntil: 'networkidle'
        });
        await expect(page).toHaveURL(routePattern(url));
        await expect(page.locator('main, [role="main"]').first()).toBeVisible();
        await audit(page, label);
    });
}

test.describe('authenticated filing screens', () => {
    test.use({
        storageState: process.env.A11Y_STORAGE_STATE
    });

    for (const [label, url] of signedInScreens) {
        test(`accessible: ${label}`, async ({
            page
        }) => {
            await page.goto(url, {
                waitUntil: 'networkidle'
            });
            await expect(page).toHaveURL(routePattern(url));
            await expect(page.locator('main, [role="main"]').first()).toBeVisible();
            await audit(page, label);
        });
    }
});

// Illinois pauses filing plans (#386), so audit the plans screen in
// Massachusetts. It needs its own session: switching jurisdiction signs out.
test.describe('authenticated filing plans', () => {
    test.use({
        storageState: process.env.A11Y_MASSACHUSETTS_STORAGE_STATE
    });

    test('accessible: filing plans', async ({
        page
    }) => {
        const url = '/jurisdiction/massachusetts/plans/';
        await page.goto(url, {
            waitUntil: 'networkidle'
        });
        await expect(page).toHaveURL(routePattern(url));
        await expect(page.locator('main, [role="main"]').first()).toBeVisible();
        await audit(page, 'filing plans');
    });
});

test.describe('authenticated dialog behavior', () => {
    test.use({
        storageState: process.env.A11Y_STORAGE_STATE
    });

    test('accessible: claim-party dialog', async ({
        page
    }) => {
        await page.goto('/jurisdiction/illinois/parties/', {
            waitUntil: 'networkidle'
        });
        const dialog = page.locator('#claim-party-dialog');
        await expect(dialog).toBeAttached();
        // Dialog copy and its triggering suggestion vary with the filing data, so
        // open the native dialog independently of either to keep this check stable.
        await dialog.evaluate(element => element.showModal());
        await expect(dialog).toBeVisible();
        await audit(page, 'claim-party dialog', '#claim-party-dialog');
        await dialog.evaluate(element => element.close());
        await expect(dialog).toBeHidden();
    });
});

test.afterAll(() => {
    const report = {
        findings
    };
    fs.mkdirSync('test-results', {
        recursive: true
    });
    fs.writeFileSync('test-results/axe-results.json', JSON.stringify(report, null, 2));
    // This is deliberately a risk gate while the existing accessibility
    // backlog is addressed. Moderate and minor WCAG findings remain in the
    // uploaded report; serious and critical findings block a pull request.
    const blocking = findings.filter(finding => blockingImpacts.has(finding.impact));
    if (blocking.length) {
        throw new Error(`${blocking.length} serious or critical WCAG A/AA Axe finding(s); see test-results/axe-results.json.`);
    }
});