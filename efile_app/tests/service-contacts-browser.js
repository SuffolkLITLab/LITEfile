const assert = require('node:assert/strict');
const fs = require('node:fs');
const {
    chromium,
    expect
} = require('@playwright/test');
const {
    default: AxeBuilder
} = require('@axe-core/playwright');
(async () => {
    const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const browser = await chromium.launch();
    try {
        for (const width of [1280, 390]) {
            const context = await browser.newContext({
                viewport: {
                    width,
                    height: width < 500 ? 844 : 1000
                }
            });
            await context.addCookies([{
                name: 'sessionid',
                value: config.cookie,
                url: config.baseUrl
            }]);
            const page = await context.newPage();
            await page.goto(config.baseUrl + config.uploadUrl, {
                waitUntil: 'domcontentloaded'
            });
            await page.evaluate(() => document.fonts.ready);
            const upload = await page.locator('#upload-button').boundingBox();
            const confirm = await page.locator('#continue-to-analysis').boundingBox();
            assert.ok(Math.abs(upload.x + upload.width - confirm.x - confirm.width) <= 1, 'Upload and Confirm share the right grid edge');
            assert.ok(Math.abs(upload.height - confirm.height) <= 1, 'Upload and Confirm use the same button height');
            if (width < 700) assert.ok(Math.abs(upload.x - confirm.x) <= 1, 'Both mobile actions span the content width');
            assert.equal(await page.locator('#ai-opt-out').evaluate(el => !!(el.compareDocumentPosition(document.querySelector('#upload-button')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
            await page.screenshot({
                path: `/tmp/litefile-upload-${width}.png`,
                fullPage: true
            });
            await page.goto(config.baseUrl + config.url);
            const section = page.locator('#email-recipients');
            const modal = page.getByRole('dialog');
            const open = () => section.locator('[data-service-open]').click();
            const alex = () => modal.getByRole('checkbox', {
                name: 'Alex Example'
            });
            const rules = () => modal.getByRole('checkbox', {
                name: 'I checked'
            });
            await expect(page.getByRole('heading', {
                name: 'How will you serve the other side?'
            })).toBeVisible();
            if (width === 1280) assert.equal(await page.locator('[name="service_method"]:checked').count(), 0);
            else await expect(page.getByRole('radio', {
                name: "I'll arrange service outside LITEfile"
            })).toBeChecked();
            await page.screenshot({
                path: `/tmp/litefile-service-question-${width}.png`,
                fullPage: true
            });
            await page.getByRole('radio', {
                name: 'Check whether I can use electronic service'
            }).check();
            await expect(section).toBeVisible();
            await expect(page.locator('#service-state-rules')).toHaveAttribute('open', '');
            assert.equal(await section.getByRole('checkbox').count(), 0);
            await open();
            await expect(modal).toBeVisible();
            await alex().check();
            await modal.getByRole('link', {
                name: 'Cancel',
                exact: true
            }).click();
            await expect(modal).toHaveCount(0);
            await expect(section.locator('[data-service-open]')).toBeFocused();
            await open();
            await expect(alex()).not.toBeChecked();
            await alex().check();
            await rules().check();
            await modal.getByRole('button', {
                name: 'Save recipients',
                exact: true
            }).click();
            await expect(modal).toHaveCount(0);
            await expect(section.getByText('Alex Example', {
                exact: true
            })).toBeVisible();
            await expect(section.getByRole('status')).toContainText('Recipients saved');
            await page.evaluate(() => document.fonts.ready);
            await section.screenshot({
                path: `/tmp/litefile-service-summary-${width}.png`
            });
            await page.screenshot({
                path: `/tmp/litefile-service-step-${width}.png`,
                fullPage: true
            });

            await open();
            await expect(rules()).toBeChecked();
            await page.evaluate(() => document.fonts.ready);
            await page.screenshot({
                path: `/tmp/litefile-service-modal-${width}.png`
            });
            assert.equal(await modal.evaluate(el => el.scrollWidth <= el.clientWidth), true);
            const audit = await new AxeBuilder({
                page
            }).include('.service-dialog').analyze();
            assert.deepEqual(audit.violations, []);
            // A failed add keeps both typed details and pending recipient edits.
            await alex().uncheck();
            await modal.getByText('Add a new contact', {
                exact: true
            }).click();
            await page.locator('#contact-first-name').fill('Jordan');
            await page.locator('#contact-last-name').fill('Example');
            await page.locator('#contact-email').fill('jordan@example.com');
            await page.evaluate(() => document.fonts.ready);
            const first = await page.locator('#contact-first-name').boundingBox();
            const last = await page.locator('#contact-last-name').boundingBox();
            const email = await page.locator('#contact-email').boundingBox();
            if (width > 700) {
                assert.ok(Math.abs(first.y - last.y) <= 1);
                assert.ok(email.width > first.width * 1.8);
            } else {
                assert.ok(last.y > first.y);
                assert.ok(email.y > last.y);
            }
            await page.screenshot({
                path: `/tmp/litefile-service-add-${width}.png`
            });

            await page.route(/\/service-contacts\/(?:\?|$)/, route => route.fulfill({
                status: 400,
                contentType: 'application/json',
                body: JSON.stringify({
                    success: false,
                    error: 'The court could not add this contact. Try again.'
                })
            }), {
                times: 1
            });
            await modal.getByRole('button', {
                name: 'Add contact',
                exact: true
            }).click();
            await expect(modal.getByRole('alert')).toContainText('could not add');
            await expect(page.locator('#contact-email')).toHaveValue('jordan@example.com');
            await expect(alex()).not.toBeChecked();
            await modal.getByRole('button', {
                name: 'Add contact',
                exact: true
            }).click();
            const jordan = modal.getByRole('checkbox', {
                name: 'Jordan Example'
            });
            await expect(jordan).toBeVisible();
            await expect(alex()).not.toBeChecked();
            await jordan.check();
            await rules().check();
            await modal.getByRole('button', {
                name: 'Save recipients',
                exact: true
            }).click();
            await expect(modal).toHaveCount(0);
            await expect(section.getByText('Jordan Example', {
                exact: true
            })).toBeVisible();
            await expect(section.getByText('Alex Example', {
                exact: true
            })).toHaveCount(0);
            await open();
            await modal.getByRole('checkbox', {
                name: 'Jordan Example'
            }).uncheck();
            await modal.getByRole('button', {
                name: 'Save recipients',
                exact: true
            }).click();
            await expect(section.getByText('No service recipients selected.', {
                exact: true
            })).toBeVisible();
            await open();
            await expect(modal).toBeVisible();
            await page.keyboard.press('Escape');
            await expect(modal).toHaveCount(0);
            await expect(section.locator('[data-service-open]')).toBeFocused();
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
            await page.getByRole('radio', {
                name: "I'll arrange service outside LITEfile"
            }).check();
            await expect(section).not.toBeVisible();
            // This recipient-only fixture has no documents. Reaching the payment
            // guard proves the choice was submitted; full state-flow tests cover Fees.
            await page.getByRole('button', {
                name: 'Continue to fees'
            }).click();
            await expect(page.getByText('Add and organize at least one document before choosing payment.')).toBeVisible();
            await context.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => {
    process.stderr.write(`${error.stack}\n`);
    process.exitCode = 1;
});