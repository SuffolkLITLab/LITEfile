const fs = require("node:fs");
const {
    chromium,
    expect
} = require("@playwright/test");

async function main() {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch();
    try {
        const context = await browser.newContext();
        await context.addCookies([{
            name: config.cookieName,
            value: config.cookie,
            url: config.baseUrl
        }]);
        const page = await context.newPage();
        let componentMode = "standard";
        await page.route("**/api/**", async route => {
            const url = route.request().url();
            let data = [];
            if (url.includes("filing-types")) data = [{
                code: "petition",
                name: "Petition"
            }, {
                code: "exhibit",
                name: "Exhibit"
            }];
            if (url.includes("filing-components")) data = [{
                code: "332",
                name: "Lead Document",
                required: true,
                allowmultiple: false,
                efspcode: "LEAD"
            }, {
                code: "331",
                name: "Attachments",
                required: false,
                allowmultiple: true,
                efspcode: "ATTACH"
            }, ];
            if (url.includes("filing-components") && componentMode === "lead-only") data = data.slice(0, 1);
            if (url.includes("filing-components") && componentMode === "custom") data.push({
                code: "333",
                name: "Supporting affidavit",
                efspcode: "AFFIDAVIT",
                required: true
            });
            if (url.includes("document-types")) data = [{
                code: "public",
                name: "Public"
            }];
            await route.fulfill({
                json: {
                    success: true,
                    data,
                    available: true,
                    message: ""
                }
            });
        });
        await page.goto(`${config.baseUrl}/jurisdiction/illinois/organize-documents/?draft=${config.draft}`);
        const lead = page.locator(`[data-document-id="${config.lead}"]`);
        const child = page.locator(`[data-document-id="${config.child}"]`);
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("332");
        await expect(child.locator('.filing-component-options')).toBeHidden();
        await expect(child.getByLabel("Document role")).toBeVisible();
        await expect(child.locator('.attachment-parent option[value=""]')).toHaveText("Lead document");
        await child.locator(".attachment-parent").selectOption(String(config.lead));
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("331");
        await expect(child.locator('.filing-component-options')).toBeHidden();
        await expect(child.locator(".filing-type")).toBeDisabled();
        await expect(lead.locator(".attachment-parent")).toBeDisabled();
        await expect(child.locator(".courtesy-email")).toHaveValue("");
        await expect(child.locator(".certified-copy-details")).toBeHidden();
        await child.locator(".attachment-parent").selectOption("");
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("332");
        await expect(child.locator(".filing-type")).toBeEnabled();
        await expect(lead.locator(".attachment-parent")).toBeEnabled();
        await child.locator(".attachment-parent").selectOption(String(config.lead));
        await lead.locator(".document-name").fill("Renamed lead");
        await expect(child.locator(`.attachment-parent option[value="${config.lead}"]`)).toHaveText("Attachment to: Renamed lead");
        await lead.locator(".filing-type").selectOption("exhibit");
        await expect(child.locator(".filing-type")).toHaveValue("exhibit");
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("331");
        await expect(page.locator('[data-choices-state="loading"]')).toHaveCount(0);
        componentMode = "lead-only";
        await child.locator(".attachment-parent").selectOption("");
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("332");
        await child.locator(".attachment-parent").selectOption(String(config.lead));
        await expect(child.locator('.filing-component-options')).toContainText("does not allow attachments");
        expect(await child.locator(".attachment-parent").evaluate(el => el.checkValidity())).toBe(false);
        componentMode = "custom";
        await child.locator(".attachment-parent").selectOption("");
        await expect(child.locator('.filing-component-options')).toBeVisible();
        await expect(child.locator('.filing-component-options input[value="333"]')).toHaveCount(1);
        await child.locator(".attachment-parent").selectOption(String(config.lead));
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("333");
        await expect(child.locator('.filing-component-options input[value="332"]')).toHaveCount(0);
        expect(await child.locator(".attachment-parent").evaluate(el => el.checkValidity())).toBe(true);
        componentMode = "standard";
        await child.locator(".attachment-parent").selectOption("");
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("332");
        await child.locator(".attachment-parent").selectOption(String(config.lead));
        await expect(child.locator('.filing-component-options input:checked')).toHaveValue("331");
        await expect(child.locator('.filing-component-options')).toBeHidden();
        if (process.env.ATTACHMENT_SCREENSHOT_PATH) {
            await page.screenshot({
                path: process.env.ATTACHMENT_SCREENSHOT_PATH,
                fullPage: true
            });
        }
        const saved = page.waitForResponse(r => r.request().method() === "POST" && r.url().includes("organize-documents"));
        await page.locator("#save-document-details").click();
        expect((await saved).status()).toBe(200);
    } finally {
        await browser.close();
    }
}
main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});