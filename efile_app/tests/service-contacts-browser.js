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
                    height: 1100
                }
            });
            await context.addCookies([{
                name: 'sessionid',
                value: config.cookie,
                url: config.baseUrl
            }]);
            const page = await context.newPage();
            await page.goto(config.baseUrl + config.uploadUrl);
            await page.evaluate(() => document.fonts.ready);
            assert.equal(await page.locator('#ai-opt-out').evaluate(el => !!(el.compareDocumentPosition(document.querySelector('#upload-button')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
            await page.screenshot({
                path: `/tmp/litefile-upload-${width}.png`,
                fullPage: true
            });
            const icon = await page.request.get(config.baseUrl + '/favicon.ico');
            assert.equal(icon.status(), 200);
            await page.goto(config.baseUrl + config.url);
            const section = page.locator('#email-recipients');
            await expect(section).toBeVisible();
            await section.getByRole('checkbox', {
                name: 'Alex Example'
            }).check();
            await section.getByRole('checkbox', {
                name: 'I checked'
            }).check();
            await section.getByRole('button', {
                name: 'Save email recipients'
            }).click();
            await expect(page.getByText('Email recipients saved.', {
                exact: false
            })).toBeVisible();
            await expect(section.getByRole('checkbox', {
                name: 'Alex Example'
            })).toBeChecked();
            await section.locator('summary').click();
            await page.evaluate(() => document.fonts.ready);
            await section.screenshot({
                path: `/tmp/litefile-service-${width}.png`
            });
            const first = await page.locator('#contact-first-name').boundingBox();
            const last = await page.locator('#contact-last-name').boundingBox();
            if (width > 700) assert.ok(Math.abs(first.y - last.y) <= 1, JSON.stringify({
                first,
                last
            }));
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
            const audit = await new AxeBuilder({
                page
            }).include('#email-recipients').analyze();
            assert.deepEqual(audit.violations, []);
            await section.screenshot({
                path: `/tmp/litefile-service-${width}.png`
            });
            await section.getByRole('button', {
                name: 'Remove all email recipients'
            }).click();
            await expect(section.getByText('No email recipients selected.', {
                exact: true
            })).toBeVisible();
            await section.locator('summary').click();
            await page.locator('#contact-first-name').fill('Jordan');
            await page.locator('#contact-last-name').fill('Example');
            await page.locator('#contact-email').fill('jordan@example.com');
            await section.getByRole('button', {
                name: 'Add contact',
                exact: true
            }).click();
            await expect(page.getByText('Contact added. Select them below to include them.')).toBeVisible();
            await section.getByRole('checkbox', {
                name: 'Jordan Example'
            }).check();
            await section.getByRole('checkbox', {
                name: 'I checked'
            }).check();
            await section.getByRole('button', {
                name: 'Save email recipients'
            }).click();
            await expect(section.getByRole('checkbox', {
                name: 'Jordan Example'
            })).toBeChecked();
            await section.getByRole('button', {
                name: 'Remove all email recipients'
            }).click();
            await context.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => {
    process.stderr.write(`${error.stack}\n`);
    process.exitCode = 1;
});