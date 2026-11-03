/* global DocumentChecks */
const PAYMENT_URLS = {
    accounts: "/api/payment-accounts/",
    accountTypes: "/api/payment-account-types/",
    token: "/api/auth/tyler-token/",
    fees: "/api/payment-fees/",
    waiver: "/api/waiver-account/"
};

// Tyler sends Active as a JAXB element ({value: true, ...}), not a bare boolean.
const isActive = (account) => {
    const active = account.active?.value ?? account.active;
    return active !== false && active !== "false";
};

const paymentJSON = (id) => {
    const element = document.getElementById(id);
    return element ? JSON.parse(element.textContent) : {};
};

const paymentMessages = {
    hide() {
        document.getElementById("errorMessage").hidden = true;
        document.getElementById("successMessage").hidden = true;
    },
    // Only a fee quote error is one the filer can go on past to Review.
    showError(message, actions = [], {
        feeQuote = false
    } = {}) {
        document.getElementById("errorText").textContent = message;
        document.getElementById("feeQuoteHelp").hidden = !feeQuote;
        window.FilingErrorActions?.render("filing-error-actions", actions);
        const box = document.getElementById("errorMessage");
        box.hidden = false;
        box.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    },
    showSuccess(message) {
        document.getElementById("successText").textContent = message;
        document.getElementById("successMessage").hidden = false;
    }
};
window.Messages = paymentMessages;

const escapeHTML = (value) => {
    const span = document.createElement("span");
    span.textContent = String(value || "");
    return span.innerHTML;
};

const escapeAttribute = (value) => escapeHTML(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");

const PaymentPage = {
    caseData: paymentJSON("case-data"),
    feeQuoteReady: false,
    // code -> the court's own name for that account type (e.g. "CC" -> "Credit
    // Card"), fetched from GetPaymentAccountTypeList. Populated by
    // loadAccountTypes(); empty if that call fails, which just means the
    // generic fallback in accountLabel() is used instead.
    typeDescriptions: {},

    setFeesState(loading) {
        document.getElementById("loadingSpinner").style.display = loading ? "block" : "none";
        // Review is also where filers correct answers that caused a fee error.
        // A confirmed quote is required for submission, not for opening Review.
        document.getElementById("submitButton").disabled = loading || this.removingAccount || this.waiverUploading || DocumentChecks.pending() || !document.getElementById("selected-payment-account").value;
    },

    // A copy added on this page is confirmed here before Review.
    async onDocumentCheck({
        action,
        data
    }) {
        // The check endpoint verified this page's fee token before changing
        // documents, so the returned token still matches our caseData.
        document.getElementById("fee-inputs-token").textContent = JSON.stringify(data.fee_inputs_token);
        const confirmation = document.getElementById("waiver-upload-confirmation");
        if (action === "remove") {
            if (confirmation) confirmation.hidden = true;
            const upload = document.getElementById("waiver-upload-required");
            if (upload) upload.hidden = false;
            document.getElementById("add-waiver-document")?.focus();
            paymentMessages.showSuccess(gettext("Document removed. You can upload a different file."));
            // The removed document no longer counts toward fees.
            await this.chooseIntent();
        } else {
            if (confirmation && !confirmation.hidden) confirmation.textContent = gettext("Fee waiver document added and checked.");
            const next = document.querySelector("[data-document-check]") || document.querySelector('input[name="paymentIntent"]:checked') || document.getElementById("submitButton");
            next.focus();
        }
        this.setFeesState(false);
    },

    async loadAccountTypes() {
        try {
            const result = await apiUtils.fetchJSON(PAYMENT_URLS.accountTypes, "GET", {
                jurisdiction: apiUtils.getCurrentJurisdiction()
            });
            const types = result?.success ? result.data : [];
            (types || []).forEach((type) => {
                if (type.code) this.typeDescriptions[type.code] = type.description || type.code;
            });
        } catch {
            // The court's own type names are a nicety, not a requirement --
            // accountLabel() falls back to the account's own name if this
            // list never loads.
        }
    },

    // Tyler's payment accounts aren't all cards -- assuming so mislabeled
    // waivers, ACH/bank accounts, and firm balances alike as "Card ending in
    // ****". Only claim "card" when the account actually carries card data;
    // otherwise use the court's own name for the account type, falling back
    // to whatever Tyler calls the account if that type list didn't load.
    accountLabel(account, waiverCount) {
        if (account.paymentAccountTypeCode === "WV") {
            return waiverCount > 1 ? `${gettext("Payment waiver")}: ${account.accountName}` : gettext("Payment waiver");
        }
        if (account.cardLast4) {
            return `${account.cardType?.value || gettext("Card")} ${gettext("ending in")} ${account.cardLast4}`;
        }
        const typeName = this.typeDescriptions[account.paymentAccountTypeCode];
        if (typeName && account.accountName) return `${typeName}: ${account.accountName}`;
        return typeName || account.accountName || gettext("Payment account");
    },

    async loadAccounts() {
        const result = await apiUtils.fetchJSON(PAYMENT_URLS.accounts, "GET", {
            jurisdiction: apiUtils.getCurrentJurisdiction()
        });
        if (!result?.success || !Array.isArray(result.data)) throw new Error("Account lookup failed");
        this.showBankPolicy(result);
        this.accounts = result.data.filter((account) => isActive(account));
        const saved = paymentJSON("selected-payment-account-id");
        const account = this.accounts.find((item) => String(item.paymentAccountID) === String(saved));
        if (document.querySelector('input[name="paymentIntent"]:checked')) return;
        let intent = account ? "pay" : "";
        // A saved waiver is not a fresh statement that the person qualifies.
        if (account?.paymentAccountTypeCode === "WV") intent = "";
        if (paymentJSON("estimated-zero-fees") === true) intent = "pay";
        if (new URLSearchParams(window.location.search).get("payment_status") === "success") intent = "pay";
        if (intent) {
            document.querySelector(`input[name="paymentIntent"][value="${intent}"]`).checked = true;
            await this.chooseIntent();
        }
    },

    async chooseIntent() {
        const intent = document.querySelector('input[name="paymentIntent"]:checked')?.value;
        const bankNotice = document.getElementById("bank-payment-notice");
        if (bankNotice) bankNotice.hidden = true;
        const requestId = this.quoteRequestId = (this.quoteRequestId || 0) + 1;
        this.feeQuoteReady = false;
        document.getElementById("selected-payment-account").value = "";
        document.getElementById("paymentSection").hidden = true;
        const container = document.getElementById("paymentMethodsContainer");
        container.innerHTML = "";
        paymentMessages.hide();
        this.setFeesState(false);
        document.getElementById("roughFeeSummary").hidden = false;
        document.getElementById("freeFilingHelp").hidden = intent !== "pay" || paymentJSON("estimated-zero-fees") !== true;
        document.getElementById("waiverHelp").hidden = intent !== "waiver";
        if (intent === "waiver") return this.chooseWaiver(requestId);
        if (intent === "pay") return this.choosePaidAccount(requestId);
    },

    async chooseWaiver(requestId) {
        this.setFeesState(true);
        try {
            // Share an in-flight creation when someone toggles back and forth.
            this.waiverRequest ||= apiUtils.post(PAYMENT_URLS.waiver, {
                preferred_id: paymentJSON("selected-payment-account-id") || ""
            }, {
                jurisdiction: apiUtils.getCurrentJurisdiction()
            });
            const result = await this.waiverRequest;
            if (!result?.success || !result.data?.paymentAccountID) throw new Error("Waiver setup failed");
            if (requestId !== this.quoteRequestId) return;
            const account = result.data;
            document.getElementById("selected-payment-account").value = account.paymentAccountID;
            document.getElementById("selected-payment-account-name").value = account.accountName;
            document.getElementById("selected-payment-account-type").value = "WV";
            this.feeQuoteReady = true;
            paymentMessages.showSuccess(paymentJSON("waiver-exemption").requires_waiver_account === true ?
                gettext("Fee exemption claimed: $0 due now. The court will review your filing.") :
                gettext("Fee waiver requested: $0 due now. The court may ask you to pay if it denies your request."));
        } catch {
            this.waiverRequest = null;
            if (requestId !== this.quoteRequestId) return;
            paymentMessages.showError(gettext("We could not set up your waiver account. Please try again."));
            this.showChoiceRetry();
        } finally {
            if (requestId === this.quoteRequestId) this.setFeesState(false);
        }
    },

    async choosePaidAccount(requestId) {
        const container = document.getElementById("paymentMethodsContainer");
        try {
            // A lookup failure is not evidence that there are no accounts.
            const result = await apiUtils.fetchJSON(PAYMENT_URLS.accounts, "GET", {
                jurisdiction: apiUtils.getCurrentJurisdiction()
            });
            if (requestId !== this.quoteRequestId) return;
            if (!result?.success || !Array.isArray(result.data)) throw new Error("Account lookup failed");
            this.showBankPolicy(result);
            const accounts = result.data.filter((account) => account.paymentAccountTypeCode !== "WV" && isActive(account));
            const saved = paymentJSON("selected-payment-account-id");
            const selectedId = accounts.some((account) => String(account.paymentAccountID) === String(saved)) ? saved : accounts[0]?.paymentAccountID;
            const rows = accounts.map((account) => {
                const label = this.accountLabel(account, 0);
                return `<div><label><input class="form-check-input" type="radio" name="paymentMethod" value="${escapeAttribute(account.paymentAccountID)}" data-name="${escapeAttribute(label)}" data-type="${escapeAttribute(account.paymentAccountTypeCode || "")}" ${String(selectedId) === String(account.paymentAccountID) ? "checked" : ""}/> <span><strong>${escapeHTML(label)}</strong></span></label> <button type="button" class="btn btn-outline-secondary btn-sm remove-payment-method" data-account-id="${escapeAttribute(account.paymentAccountID)}" data-account-label="${escapeAttribute(label)}" aria-label="${escapeAttribute(gettext("Remove payment method"))}: ${escapeAttribute(label)}">${gettext("Remove")}</button></div>`;
            }).join("");
            const bankUnavailable = result.bank_payment_policy && result.bank_payment_policy !== "allowed";
            const addLabel = bankUnavailable ? gettext("Add credit card") : gettext("Add credit or bank account");
            const addHelp = bankUnavailable ? gettext("Add a credit card to continue.") : gettext("Add a credit or bank account to continue.");
            const noAccountHelp = accounts.length ? "" : `<p>${addHelp}</p>`;
            container.innerHTML = `${noAccountHelp}<div class="compact-choice-list">${rows}</div>
                <button type="button" class="btn btn-outline-primary mt-2" id="add-payment-method" aria-haspopup="dialog">${addLabel}</button>`;
            container.querySelectorAll('input[name="paymentMethod"]').forEach((input) => {
                input.addEventListener("change", () => this.selectAndQuote());
            });
            container.querySelectorAll(".remove-payment-method").forEach((button) => {
                button.addEventListener("click", () => this.removeAccount(button));
            });
            document.getElementById("add-payment-method").addEventListener("click", () => document.getElementById("payment-gateway-dialog").showModal());
            if (accounts.length) await this.selectAndQuote();
        } catch {
            if (requestId === this.quoteRequestId) {
                paymentMessages.showError(gettext("We could not load payment methods. Please try again."));
                this.showChoiceRetry();
            }
        }
    },

    showBankPolicy(result) {
        const notice = document.getElementById("bank-payment-notice");
        if (!notice) return;
        notice.hidden = !result.bank_payment_message || document.querySelector('input[name="paymentIntent"]:checked')?.value !== "pay";
        document.getElementById("bank-payment-message").textContent = result.bank_payment_message || "";
        const retry = document.getElementById("retry-bank-policy");
        retry.hidden = result.bank_payment_policy !== "unknown";
        retry.onclick = () => {
            document.querySelector('input[name="paymentIntent"][value="pay"]').checked = true;
            return this.chooseIntent();
        };
        document.getElementById("paid-account-label").textContent = result.bank_payment_policy && result.bank_payment_policy !== "allowed" ? gettext("Use a credit card") : gettext("Use a credit or bank account");
    },

    showChoiceRetry() {
        const container = document.getElementById("paymentMethodsContainer");
        container.innerHTML = `<button type="button" class="btn btn-outline-primary" id="retry-payment-choice">${gettext("Try again")}</button>`;
        document.getElementById("retry-payment-choice").addEventListener("click", () => this.chooseIntent());
    },

    async selectAndQuote() {
        const selected = document.querySelector('input[name="paymentMethod"]:checked');
        if (!selected) return;
        this.quoteRequestId = (this.quoteRequestId || 0) + 1;
        const currentRequestId = this.quoteRequestId;
        document.getElementById("selected-payment-account").value = selected.value;
        document.getElementById("selected-payment-account-name").value = selected.dataset.name;
        document.getElementById("selected-payment-account-type").value = selected.dataset.type || "";
        document.getElementById("paymentSection").hidden = true;
        this.feeQuoteReady = false;
        document.getElementById("roughFeeSummary").hidden = false;
        paymentMessages.hide();
        this.setFeesState(true);
        try {
            const uploadData = await apiUtils.getUploadData();
            if (currentRequestId !== this.quoteRequestId) return;
            const userData = FilingPayload.userDataFromCaseData(this.caseData);
            const efileData = this.buildEFilingData(userData, this.caseData, uploadData, selected.value);
            const result = await apiUtils.post(PAYMENT_URLS.fees, {
                efile_data: efileData,
                confirm_submission: true,
                payment_account_id: selected.value,
                fee_inputs_token: paymentJSON("fee-inputs-token")
            }, {}, {
                timeout: ApiUtils.FEE_TIMEOUT_MS
            });
            if (currentRequestId !== this.quoteRequestId) return;
            // The server keeps the quote for Review once it can read a total
            // from it; one it could not read is not a quote to go on with.
            this.feeQuoteReady = Boolean(result?.success && result.quote_recorded);
            this.handleFeesResponse(result);
            document.getElementById("paymentSection").hidden = !this.feeQuoteReady;
            document.getElementById("roughFeeSummary").hidden = this.feeQuoteReady;
            document.getElementById("freeFilingHelp").hidden = this.feeQuoteReady || paymentJSON("estimated-zero-fees") !== true;
            const amount = result?.api_response?.feesCalculationAmount?.value;
            if (this.feeQuoteReady && amount != null && String(amount).trim() !== "" && Number(amount) === 0) {
                paymentMessages.showSuccess(gettext("You will not be charged."));
            }
            if (result?.success && result.quote_superseded) {
                paymentMessages.showError(gettext("This filing changed while we were calculating fees, perhaps in another window. Reload this page to calculate them again."));
            } else if (result?.success && !result.quote_recorded) {
                paymentMessages.showError(gettext("The court did not return a fee total for this filing. Try again, or contact the court before you file."), [], {
                    feeQuote: true
                });
            }
        } catch (error) {
            if (currentRequestId !== this.quoteRequestId) return;
            this.feeQuoteReady = false;
            paymentMessages.showError(error?.serverMessage || gettext("We could not calculate fees. Please try again."), error?.errorActions, {
                feeQuote: true
            });
            this.setFeesState(false);
        }
    },

    async removeAccount(button) {
        if (this.removingAccount) return;
        if (!window.confirm(`${gettext("Remove this payment method from your court account?")}\n${button.dataset.accountLabel}`)) return;
        this.removingAccount = true;
        button.disabled = true;
        this.setFeesState(false);
        paymentMessages.hide();
        try {
            const result = await apiUtils.delete(`${PAYMENT_URLS.accounts}${encodeURIComponent(button.dataset.accountId)}/`, {
                jurisdiction: apiUtils.getCurrentJurisdiction()
            });
            if (!result?.success) throw new Error("Account removal failed");
            // Keep the usable selection and quote until removal succeeds.
            this.quoteRequestId = (this.quoteRequestId || 0) + 1;
            this.feeQuoteReady = false;
            document.getElementById("selected-payment-account").value = "";
            document.getElementById("paymentSection").hidden = true;
            // Do not let the initial saved selection choose the removed account.
            document.getElementById("selected-payment-account-id").textContent = '""';
            await this.chooseIntent();
            paymentMessages.showSuccess(gettext("Payment method removed."));
        } catch {
            paymentMessages.showError(gettext("We could not remove this payment method. Please try again."));
        } finally {
            this.removingAccount = false;
            button.disabled = false;
            this.setFeesState(false);
        }
    },

    async addAccount() {
        const authData = await apiUtils.fetchJSON(PAYMENT_URLS.token, "GET", {
            jurisdiction: apiUtils.getCurrentJurisdiction()
        });
        if (!authData?.success || !authData.data?.tyler_token) {
            paymentMessages.showError(gettext("We could not verify your account. Please sign in again."));
            return;
        }
        const jurisdiction = authData.data.state || apiUtils.getCurrentJurisdiction();
        const form = document.createElement("form");
        form.method = "post";
        form.action = paymentJSON("new-toga-url");
        const fields = {
            account_name: `Payment account made on ${new Date().toDateString()}`,
            global: "false",
            type_code: "CC",
            tyler_info: authData.data.tyler_token,
            original_url: window.withFilingDraft(`${window.location.origin}/jurisdiction/${jurisdiction}/payment/?payment_status=success`),
            error_url: window.withFilingDraft(`${window.location.origin}/jurisdiction/${jurisdiction}/payment/?payment_status=failure`)
        };
        Object.entries(fields).forEach(([name, value]) => {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            input.value = value;
            form.appendChild(input);
        });
        document.body.appendChild(form);
        form.submit();
    },

    async init() {
        const gatewayDialog = document.getElementById("payment-gateway-dialog");
        document.getElementById("cancel-payment-gateway").addEventListener("click", () => gatewayDialog.close());
        document.getElementById("confirm-payment-gateway").addEventListener("click", async () => {
            if (this.addingAccount) return;
            this.addingAccount = true;
            gatewayDialog.close();
            const button = document.getElementById("add-payment-method");
            const label = button.textContent;
            button.disabled = true;
            button.textContent = gettext("Opening payment provider…");
            try {
                await this.addAccount();
            } catch {
                paymentMessages.showError(gettext("We could not open the payment provider’s website. Please try again."));
            } finally {
                this.addingAccount = false;
                button.disabled = false;
                button.textContent = label;
                button.focus();
            }
        });
        const status = new URLSearchParams(window.location.search).get("payment_status");
        if (status === "failure") paymentMessages.showError(gettext("The payment method was not added."));
        if (status === "success") paymentMessages.showSuccess(gettext("Payment method added."));
        document.querySelectorAll('input[name="paymentIntent"]').forEach((input) => {
            input.addEventListener("change", () => this.chooseIntent());
        });
        document.getElementById("document-checks").addEventListener("document-checks:change", (event) => this.onDocumentCheck(event.detail));
        await this.loadAccountTypes();
        this.loadAccounts().catch(() => paymentMessages.showError(gettext("We could not load payment methods.")));
    }
};

Object.assign(PaymentPage, FilingPayload);
document.addEventListener("DOMContentLoaded", () => PaymentPage.init());