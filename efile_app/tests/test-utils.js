/**
 * Test Utilities
 * 
 * Common utilities and configuration for Playwright tests.
 * Import this in your test files instead of duplicating environment setup.
 */

const {
    expect
} = require('@playwright/test');

/**
 * Get test configuration from environment variables
 * @returns {Object} Test configuration object
 */
function getTestConfig(jurisdiction = 'illinois') {
    // Same Tyler test-EFM login the Python suite uses (efile/tests/) and that
    // CI supplies from secrets -- one name, so a working .env works everywhere.
    // Other states have their own test accounts, prefixed with the state.
    const prefix = jurisdiction === 'illinois' ? '' : `${jurisdiction.toUpperCase()}_`;
    const username = process.env[`${prefix}TESTS_TYLER_USERNAME`];
    const password = process.env[`${prefix}TESTS_TYLER_PASSWORD`];
    const baseUrl = process.env.E2E_TEST_BASE_URL || 'http://localhost:8000';

    if (!username || !password) {
        throw new Error(`${prefix}TESTS_TYLER_USERNAME and ${prefix}TESTS_TYLER_PASSWORD must be set in .env file`);
    }

    return {
        username,
        password,
        baseUrl,
        jurisdiction
    };
}

async function waitForLogin(page) {
    try {
        await page.waitForURL(/\/options\/?$/, {
            timeout: 120000
        });
    } catch (error) {
        const alert = page.locator('[role="alert"]').first();
        const detail = await alert.isVisible().catch(() => false) ?
            `: ${await alert.innerText()}` :
            '';
        throw new Error(`Login did not reach the filing options page${detail}`, {
            cause: error
        });
    }
}

/**
 * Common login flow for tests (via logout page - ensures clean session)
 * @param {Page} page - Playwright page object
 * @param {Object} config - Test configuration
 */
async function loginViaLogout(page, config = getTestConfig()) {
    console.log(`Logging in as ${config.username} to ${config.baseUrl}`);

    // Configure context to preserve session cookies
    await page.context().addCookies([]);

    // Navigate to logout page
    await page.goto(`${config.baseUrl}/jurisdiction/${config.jurisdiction || 'illinois'}/logout`);

    // Fill in login credentials
    await page.getByLabel('Email address').fill(config.username);
    await page.getByLabel('Password').fill(config.password);

    // Submit the form
    await page.getByRole('button', {
        name: /Sign In/i
    }).click();

    // Wait for navigation to complete
    await waitForLogin(page);
}

/**
 * Common login flow for tests (via login page)
 * @param {Page} page - Playwright page object
 * @param {Object} config - Test configuration
 */
async function loginViaLoginPage(page, config = getTestConfig()) {
    console.log(`Logging in as ${config.username} to ${config.baseUrl}`);

    // Navigate to login page
    await page.goto(`${config.baseUrl}/jurisdiction/${config.jurisdiction || 'illinois'}/login`);

    // Fill in login credentials
    await page.getByLabel('Email address').fill(config.username);
    await page.getByLabel('Password').fill(config.password);

    // Submit the form
    await page.getByRole('button', {
        name: /Sign In/i
    }).click();

    // Wait for navigation to complete
    await waitForLogin(page);
}

/**
 * Leave Upload once the first file has been read, through Preview to Confirm
 * case. Confirm case sends a filer back while analysis is still running, so
 * wait for the analysis first, then confirm the prepared PDFs on Preview.
 */
async function continueFromUpload(page, timeout = 300000) {
    await expect(page.locator('.status-pill--analyzing')).toHaveCount(0, {
        timeout
    });
    const next = page.locator('#continue-to-analysis');
    await expect(next).toHaveAttribute('href', /.+/, {
        timeout
    });
    await Promise.all([
        page.waitForURL(/\/(preview-documents|extraction-review)\//, {
            timeout: 120000
        }),
        next.click(),
    ]);
    if (/\/preview-documents\//.test(page.url())) {
        await Promise.all([
            page.waitForURL(/\/extraction-review\//, {
                timeout: 120000
            }),
            page.getByRole('button', {
                name: 'Continue',
                exact: true
            }).click(),
        ]);
    }
}

/**
 * Fill the named fields, then any other visible required text field. A field
 * may be a select (State is), which takes an option value instead of text.
 */
async function fillRequiredInputs(page, values) {
    for (const [name, value] of Object.entries(values)) {
        const field = page.locator(`[name="${name}"]:visible`).first();
        if (!(await field.count())) continue;
        if ((await field.evaluate((element) => element.tagName)) === 'SELECT') await field.selectOption(value);
        else await field.fill(value);
    }
    await fillOtherRequiredInputs(page);
}

async function fillOtherRequiredInputs(page) {
    const required = page.locator('input[required]:visible');
    for (let index = 0; index < (await required.count()); index += 1) {
        const input = required.nth(index);
        const type = await input.getAttribute('type');
        if (type === 'radio' || type === 'checkbox' || (await input.inputValue())) continue;
        await input.fill(type === 'email' ? 'efile-test@example.com' : type === 'number' ? '1' : 'Test value');
    }
}

/**
 * Pay from a saved account on Fees. The intent radios are only wired up once
 * the page has loaded the filer's accounts, so wait for that before choosing.
 */
async function choosePayByAccount(page) {
    await page.waitForFunction('typeof PaymentPage !== "undefined" && Array.isArray(PaymentPage.accounts)', null, {
        timeout: 120000
    });
    await page.locator('input[name="paymentIntent"][value="pay"]').check();
    await expect(page.locator('input[name="paymentMethod"]')).not.toHaveCount(0, {
        timeout: 120000
    });
}

/**
 * Submit the extraction review after answering its conditional questions.
 * The filer-side question only appears for case types whose checklist differs
 * by side, and it is populated asynchronously after the taxonomy choices.
 */
async function continueFromExtractionReview(page, nextUrl = /\/(document-checklist|case-lookup)\//) {
    const roleField = page.locator('#filer-role-field');
    if (await roleField.count()) {
        await page.waitForLoadState('networkidle', {
            timeout: 120000
        });
        if (await roleField.isVisible()) {
            await roleField.locator('input[name="filer_role"]').first().check();
        }
    }

    await Promise.all([
        page.waitForURL(nextUrl, {
            timeout: 120000
        }),
        page.getByRole('button', {
            name: 'Continue',
            exact: true
        }).click(),
    ]);
}

/**
 * Confirm the document checklist, first resolving a filer-side question if a
 * caller reached this page from an older or incomplete draft.
 */
async function continueFromDocumentChecklist(page) {
    const continueButton = page.getByRole('button', {
        name: /Continue to organize/i
    });
    const role = page.locator('input[name="filer_role"]:visible').first();
    if (await role.count()) {
        await role.check();
        await page.getByRole('button', {
            name: /Show my documents/i
        }).click();
        await continueButton.waitFor({
            state: 'visible',
            timeout: 120000
        });
    }

    // The checklist is a guide, not a gate: continue without ticking anything.
    await Promise.all([
        page.waitForURL(/\/organize-documents\//, {
            timeout: 120000
        }),
        continueButton.click(),
    ]);
}

/**
 * Answer new or existing case on Confirm case. A saved answer is shown as a
 * summary with Change, and only opened when it has to change.
 */
async function chooseFilingPath(page, value) {
    const radio = page.locator(`input[type="radio"][name="existing_case"][value="${value}"]`);
    if (!(await radio.isVisible())) {
        if (await radio.isChecked()) return;
        await page.locator('#change-filing-path').click();
    }
    await radio.check();
}

/**
 * Choose a known court through the jurisdiction's visible guided questions.
 * Asking the selector endpoint for the route keeps state-specific court logic
 * out of the browser suite; the test still answers each rendered control.
 */
async function selectGuidedCourt(page, jurisdiction, courtCode, selectId = 'court_code') {
    const response = await page.request.get('/api/dropdowns/court-selector/', {
        params: {
            jurisdiction,
            court: courtCode
        }
    });
    if (!response.ok()) throw new Error(`Could not resolve the guided route for court ${courtCode}`);
    const payload = await response.json();
    const data = payload.data || {};
    if (!payload.success || !data.available) {
        throw new Error(`No guided court selector is available for ${jurisdiction}`);
    }

    const selector = page.locator('#court-selector');
    const settledSelector = () => expect(selector).not.toHaveAttribute('aria-busy', 'true', {
        timeout: 120000
    });
    for (const step of data.steps || []) {
        if (!step.answer) continue;
        if (step.type === 'location') continue;

        // A short list is radios and a long one a dropdown; a question already
        // answered is folded to a line with Change.
        const radio = selector.locator(`input[type="radio"][data-step="${step.id}"][value="${step.answer}"]`);
        const select = selector.locator(`select[data-step="${step.id}"]`);
        const folded = selector.locator(`[data-change="${step.id}"]`);
        await settledSelector();
        await expect(radio.or(select).or(folded).first()).toBeVisible({
            timeout: 120000
        });
        if (!(await radio.isVisible()) && !(await select.isVisible())) {
            await folded.click();
            await expect(radio.or(select).first()).toBeVisible();
        }
        if (await radio.isVisible()) await radio.check();
        else await select.selectOption(step.answer);
    }

    // Changing a court that was already settled waits for Update court.
    await settledSelector();
    const apply = selector.locator('[data-court-apply]');
    if (await apply.isVisible()) await apply.click();

    // The selector writes its answer into the page's own court <select>:
    // court_code on Confirm information, court on Case lookup.
    await page.locator(`#${selectId}`).waitFor({
        state: 'attached'
    });
    await page.waitForFunction(([id, expected]) => document.getElementById(id)?.value === expected, [selectId, courtCode], {
        timeout: 120000
    });
}

// Alias for backward compatibility
const loginUser = loginViaLogout;

module.exports = {
    getTestConfig,
    loginUser,
    loginViaLogout,
    loginViaLoginPage,
    continueFromUpload,
    fillRequiredInputs,
    choosePayByAccount,
    continueFromExtractionReview,
    continueFromDocumentChecklist,
    chooseFilingPath,
    selectGuidedCourt
};