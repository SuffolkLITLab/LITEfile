/**
 * Shared filing-payload construction.
 *
 * The review page and the payment page each build the exact same `efile_data`
 * blob for the EFSP -- the payment page to quote fees, the review page to quote
 * fees and to submit. These functions were duplicated verbatim in payment.js and
 * review.js; a fix applied to one and not the other produced a fee quote that
 * disagreed with the filing, so they live here once.
 *
 * Mixed into each page's `FilingHandler` with Object.assign, so `this` still
 * refers to the host handler and page-specific hooks (`setFeesState`,
 * `setSubmissionState`) resolve normally.
 *
 * Depends on the page-level globals `Messages`, `apiUtils`, and `gettext`.
 */
/**
 * Normalise a filing component to its bare code.
 *
 * The upload page stores the selection as `{id, name}` on the file record while
 * the durable draft stores a plain code string, so both shapes reach here. Note
 * `typeof null === "object"`, hence the explicit truthiness check -- an older
 * session with `filing_component: null` would otherwise throw on `.id`.
 *
 * @param {Object|string|null|undefined} value
 * @returns {string} the code, or "" when there isn't one
 */
function componentCode(value) {
    if (value && typeof value === "object") {
        return value.id || value.code || "";
    }
    return value || "";
}

const FilingPayload = {
    userDataFromCaseData(caseData) {
        const filer = (caseData.filing_parties || []).find((party) => party.role === "filer") || {};
        const fullName = [filer.first_name, filer.middle_name, filer.last_name]
            .filter(Boolean)
            .join(" ") || [caseData.petitioner_first_name, caseData.petitioner_last_name].filter(Boolean).join(" ");
        return {
            fullName,
            address: filer.address_line_1 || caseData.petitioner_address || "",
            addressLine2: filer.address_line_2 || "",
            city: filer.city || "",
            state: filer.state || "",
            zip: filer.zip_code || "",
            email: filer.email || caseData.petitioner_email || "",
            phone: filer.phone || caseData.petitioner_phone || ""
        };
    },

    partyFromDraft(party) {
        if (party.source === "court" && !party.external_party_id) {
            throw new Error("Reload the court case: an existing party has no court ID.");
        }
        const address = {
            address: party.address_line_1 || "",
            unit: party.address_line_2 || "",
            city: party.city || "",
            state: party.state || "",
            zip: party.zip_code || "",
            country: party.country || "US"
        };
        return {
            ...(party.source === "added" ? {
                _draft_party_id: party.id
            } : {}),
            party_type: party.party_type,
            // An organization has one name where a person has three, and the
            // EFSP only reads `name.first` that way when the entry says it is
            // a business. Without this a company reaches Tyler as a person
            // with no surname, and the court rejects the whole envelope --
            // "PersonSurName is required or does not match regular
            // expression" -- as far along as the fee quote.
            ...(party.organization_name ? {
                person_type: "business"
            } : {}),
            name: {
                // Matches person_type above: an organization name wins over
                // a person's first name left over from before the switch.
                first: party.organization_name || party.first_name || "",
                middle: party.middle_name || "",
                last: party.last_name || "",
                suffix: party.suffix || ""
            },
            // Tyler validates a present address object, even when all its
            // values are blank. Omit it when the optional address is wholly
            // blank; otherwise staging rejects the blank state as a bad code.
            ...([address.address, address.unit, address.city, address.state, address.zip].some(Boolean) ? {
                address
            } : {}),
            email: party.email || "",
            phone_number: party.phone || "",
            ...(party.external_party_id ? {
                tyler_id: party.external_party_id
            } : {}),
            is_new: !party.external_party_id
        };
    },

    /** Split a single display name into the parts Tyler asks for. */
    splitName(fullName) {
        const parts = String(fullName || "").split(" ");
        return {
            firstName: parts[0] || "",
            lastName: parts.length > 1 ? parts[parts.length - 1] : "",
            middleName: parts.length > 2 ? parts.slice(1, -1).join(" ") : ""
        };
    },

    /**
     * Who this filing is made on behalf of, out of the draft's own parties.
     *
     * Being the filer and being a party are different things: someone can
     * file for a party they are not (a parent for a child, a neighbour
     * helping with an eviction answer), and then it is that party Tyler must
     * be told the filing is for. A draft saved before that question existed
     * has nothing marked, and back then the filer was the only thing a filing
     * party could be.
     *
     * @returns {Array} the marked parties, or the filer, or nothing at all
     */
    resolveFilingParties(durableParties, durableFiler) {
        const marked = durableParties.filter((party) => party.is_filing_party && party.party_type);
        if (marked.length) {
            return marked;
        }
        return durableFiler?.party_type ? [durableFiler] : [];
    },

    /** The signed-in filer as a party, from the account they confirmed. */
    accountUser(userData, partyType) {
        const {
            firstName,
            middleName,
            lastName
        } = this.splitName(userData.fullName);
        return {
            mobile_number: userData.phone,
            phone_number: userData.phone,
            address: {
                address: userData.address,
                unit: userData.addressLine2,
                city: userData.city,
                state: userData.state,
                zip: userData.zip,
                country: "US"
            },
            email: userData.email,
            party_type: partyType,
            date_of_birth: "",
            // The authenticated account is a firm filer, not a self-represented
            // party. Marking the filer as the party makes Tyler reject fees with
            // "Cannot specify self as party for a firm filer."
            is_form_filler: false,
            name: {
                first: firstName,
                middle: middleName,
                last: lastName,
                suffix: ""
            },
            is_new: true
        };
    },

    /**
     * A party the filing is made on behalf of, who is not the person filing.
     *
     * Same shape as the signed-in filer's own entry, because Tyler makes no
     * distinction: `users` is the list of filing parties, whoever they are.
     *
     * @param {Object} party a durable draft party row
     * @param {string} noticeEmail where notices about this case should go
     */
    filingPartyFromDraft(party, noticeEmail) {
        const base = this.partyFromDraft(party);
        return {
            ...base,
            mobile_number: base.phone_number,
            date_of_birth: "",
            is_form_filler: false,
            // Tyler rejects a new case whose first filing party has no email,
            // and someone filing for another person often does not have one
            // for them. The notice address is the answer they gave to exactly
            // that question, on the parties screen, and it is theirs to change.
            email: base.email || noticeEmail
        };
    },

    buildEFilingData(userData, caseData, uploadData, paymentAccountID) {
        const {
            firstName,
            middleName,
            lastName
        } = this.splitName(userData.fullName);

        const durableParties = caseData.filing_parties || [];
        const durableFiler = durableParties.find((party) => party.role === "filer");
        const partyType = durableFiler?.party_type || caseData.determined_party_type ||
            caseData.petitioner_party_type || caseData.party_type;

        // A draft older than durable party rows has none of them, and says who
        // it is for with `partyType` alone.
        const filingParties = this.resolveFilingParties(durableParties, durableFiler);
        const filerIsFilingParty = filingParties.length ?
            filingParties.some((party) => party.role === "filer") :
            Boolean(partyType);

        if (!filingParties.length && !partyType) {
            throw new Error('Party type could not be determined. This is required for eFiling.');
        }

        // Where the court should write about this case: the filer's own
        // address, unless someone filing for another person said otherwise.
        const noticeEmail = caseData.notice_email || userData.email;

        // The filer's own entry, built only when they are a party themselves.
        const mainUser = filerIsFilingParty ? this.accountUser(userData, partyType) : null;
        const users = filingParties.length ?
            filingParties.map((party) => (
                party.role === "filer" && !party.external_party_id ? mainUser : this.filingPartyFromDraft(party, noticeEmail)
            )) : [mainUser];

        // Add second user if needed for name changes
        if (caseData.new_name_party_type && mainUser) {
            users.push({
                ...mainUser,
                party_type: caseData.new_name_party_type,
                name: {
                    first: caseData.new_first_name || firstName,
                    middle: caseData.new_middle_name || middleName,
                    last: caseData.new_last_name || lastName,
                    suffix: caseData.new_suffix || ""
                }
            });
        }

        // Add second user if needed for name changes
        const respondentPartyType = caseData.respondent_name_party_type ||
            caseData.respondent_party_type || caseData.defendant_party_type;
        if (respondentPartyType) {
            users.push({
                party_type: respondentPartyType,
                name: {
                    first: caseData.respondent_first_name || caseData.defendant_first_name || "",
                    middle: caseData.respondent_middle_name || caseData.defendant_middle_name || "",
                    last: caseData.respondent_last_name || caseData.defendant_last_name || "",
                    suffix: caseData.respondent_suffix || caseData.defendant_suffix || ""
                },
                is_new: true,
            });
        }

        let other_parties = durableParties
            .filter((party) => party.role !== "filer" && party.party_type && !filingParties.includes(party))
            .map((party) => this.partyFromDraft(party));

        if (!durableParties.length && other_parties.length === 0 && caseData.other_first_name && caseData.other_party_type) {
            const legacyAddress = {
                address: caseData.other_address_line_1 || "",
                unit: caseData.other_address_line_2 || "",
                city: caseData.other_address_city || "",
                state: caseData.other_address_state || "",
                zip: caseData.other_address_zip || "",
                country: "US"
            };
            other_parties.push({
                party_type: caseData.other_party_type,
                name: {
                    first: caseData.other_first_name,
                    last: caseData.other_last_name
                },
                ...([legacyAddress.address, legacyAddress.unit, legacyAddress.city, legacyAddress.state, legacyAddress.zip]
                    .some(Boolean) ? {
                        address: legacyAddress
                    } : {}),
                email: caseData.other_email,
                phone_number: caseData.other_phone_number,
                is_new: true,
            });
        }

        const efilingData = {
            efile_case_category: caseData.case_category,
            efile_case_type: caseData.case_type,
            efile_case_subtype: caseData.case_subtype,
            previous_case_id: caseData?.previous_case_id,
            docket_number: caseData?.previous_case_id ? caseData?.docket_number : undefined,
            users,
            other_parties,
            user_started_case: !caseData?.previous_case_id,
            al_court_bundle: [],
            comments_to_clerk: "",
            tyler_payment_id: paymentAccountID,
            // Only sent when a chosen filing type requires it (case_questions
            // asks for it in that case); the EFSP rejects the filing outright
            // if it's required and missing, so leave it out rather than send 0.
            ...(caseData?.amount_in_controversy ? {
                amount_in_controversy: caseData.amount_in_controversy
            } : {}),
            // "Someone to contact about this case", in the EFSP's own words.
            // The person filing, at whichever address they said notices about
            // the case should reach.
            lead_contact: {
                name: {
                    first: firstName,
                    middle: middleName,
                    last: lastName
                },
                email: noticeEmail
            },
            return_date: ""
        };

        this.addLowerCourt(efilingData, caseData);

        // Add court bundles for documents
        this.addCourtBundles(efilingData, uploadData, caseData, users);

        return efilingData;
    },

    addLowerCourt(efilingData, caseData) {
        if (!caseData.previous_case_id && caseData.existing_case !== "existing" && caseData.lower_court_code) {
            efilingData.lower_court_case = {
                title: caseData.lower_court_title || "",
                docket_number: caseData.lower_court_docket_number || "",
                judge: caseData.lower_court_judge || ""
            };
            efilingData.trial_court = {
                name: caseData.lower_court_name,
                tyler_lower_court_code: caseData.lower_court_code,
                tyler_prod_lower_court_code: caseData.lower_court_prod_code
            };
        }
    },

    addCourtBundles(efilingData, uploadData, caseData, users) {
        const courtName = caseData.court_name || caseData.court || "";
        // Cook and DuPage require a local attorney/SRL identifier. The code-list
        // entry is the map key; 99500 is the value both courts publish for a
        // self-represented filer. The two courts use different entry codes.
        if (courtName.toLowerCase().includes("cook")) {
            efilingData.cross_references = {
                254500: "99500"
            };
        } else if (courtName.toLowerCase().includes("dupage")) {
            efilingData.cross_references = {
                136524: "99500"
            };
        }

        // Add lead document
        if (uploadData?.files?.lead) {
            const leadBundle = this.createDocumentBundle(
                uploadData.files.lead,
                uploadData.lead_filing_type || caseData.filing_type,
                uploadData.lead_document_type || caseData.document_type,
                uploadData.lead_filing_component || caseData.filing_component,
                users,
                uploadData.lead_filing_type_name || caseData.case_type_name,
                uploadData.lead_document_type_name || "",
                uploadData.lead_cc_email,
                uploadData.lead_optional_services || uploadData.files.lead.optional_services || []
            );
            efilingData.al_court_bundle.push(leadBundle);
        }

        // Add supporting documents
        if (uploadData?.files?.supporting?.length > 0) {
            uploadData.files.supporting.forEach((doc, index) => {
                const config = uploadData.supporting_documents?.[index] || {};
                const filingComponent = componentCode(config.filing_component) ||
                    componentCode(doc.filing_component);
                const bundle = this.createDocumentBundle(
                    doc,
                    config.filing_type || caseData.filing_type_id,
                    config.document_type || caseData.document_type,
                    filingComponent || caseData.filing_component,
                    users,
                    config.filing_type_name || `Supporting Document ${index + 1}`,
                    config.document_type_name || "",
                    config.cc_email,
                    config.optional_services || config.requested_optional_services || doc.optional_services || []
                );
                efilingData.al_court_bundle.push(bundle);
            });
        }
    },

    createDocumentBundle(doc, filingType, documentType, filingComponent, users, description, docDescription, cc_email, optionalServices = []) {
        const courtesy_copies = cc_email ? [cc_email] : [];
        return {
            document_id: doc.document_id,
            proxy_enabled: true,
            filing_type: filingType,
            optional_services: optionalServices,
            due_date: null,
            filing_description: description,
            reference_number: "",
            filing_attorney: "",
            filing_comment: "",
            courtesy_copies: courtesy_copies,
            preliminary_copies: [],
            filing_parties: users.map((_user, index) => `users[${index}]`),
            filing_action: "efile",
            tyler_merge_attachments: false,
            document_type: documentType,
            filing_component: filingComponent,
            filename: doc.name,
            document_description: docDescription,
            data_url: doc.url || doc.s3_url || doc.file_url || doc.download_url
        };
    },

    handleSubmissionResult(result) {
        if (result?.success) {
            Messages.showSuccess(gettext("Filing submitted successfully! You will be redirected to the confirmation page."));
            setTimeout(() => {
                const jurisdiction = apiUtils.getCurrentJurisdiction();
                window.location.href = result.redirect_url || `/jurisdiction/${jurisdiction}/filing-confirmation/`;
            }, 2000);
        } else {
            Messages.showError(result?.error || "An error occurred during submission.", result?.error_actions);
            this.setSubmissionState(false);
        }
    },

    feeReceiptRow(label, amount, isTotal = false) {
        const row = document.createElement("div");
        row.className = isTotal ? "fee-receipt-total" : "fee-receipt-row";
        const term = document.createElement("dt");
        term.textContent = label;
        const value = document.createElement("dd");
        value.textContent = Number(amount).toLocaleString("en-US", {
            style: "currency",
            currency: "USD"
        });
        row.append(term, value);
        return row;
    },

    handleFeesResponse(result) {
        if (result?.success) {
            const response = result.api_response || {};
            const infoElem = document.getElementById("paymentInfo");
            infoElem.replaceChildren();
            const receipt = document.createElement("dl");
            receipt.className = "fee-receipt";
            const totalAmount = response.feesCalculationAmount?.value;
            const fees = (response.allowanceCharge || []).filter((fee) => fee.chargeIndicator?.value);
            if (Number(totalAmount) !== 0) {
                fees.forEach((fee) => {
                    receipt.appendChild(this.feeReceiptRow(
                        fee.allowanceChargeReason?.value || gettext("Court fee"),
                        fee.amount?.value || "0.00"
                    ));
                });
            }
            receipt.appendChild(this.feeReceiptRow(gettext("Total"), totalAmount || "0.00", true));
            infoElem.appendChild(receipt);
            document.getElementById("paymentSection").removeAttribute("hidden");
        } else {
            Messages.showError(result?.error || "An error occurred when calculating fees.", result?.error_actions);
        }
        this.setFeesState(false);
    }
};

if (typeof module !== "undefined" && module.exports) {
    module.exports = FilingPayload;
} else {
    window.FilingPayload = FilingPayload;
}