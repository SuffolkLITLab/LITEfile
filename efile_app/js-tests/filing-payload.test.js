const test = require("node:test");
const assert = require("node:assert");

const FilingPayload = require("../efile/static/js/filing-payload.js");

// The module reads these page-level globals at call time.
global.Messages = {
    showError() {},
    showSuccess() {}
};
global.gettext = (s) => s;
global.apiUtils = {
    getCurrentJurisdiction: () => "illinois"
};

/** A FilingHandler stand-in, mixed the same way the real pages mix it. */
function makeHandler() {
    const handler = {
        setFeesState() {},
        setSubmissionState() {}
    };
    return Object.assign(handler, FilingPayload);
}

const CASE_DATA = {
    filing_component: "999",
    filing_type_id: "27965",
    document_type: "dt"
};

function bundlesFor(uploadData) {
    const handler = makeHandler();
    const efilingData = {
        al_court_bundle: []
    };
    handler.addCourtBundles(efilingData, uploadData, CASE_DATA, []);
    return efilingData.al_court_bundle;
}

test("supporting component given as a plain code string is used as-is", () => {
    const bundles = bundlesFor({
        files: {
            supporting: [{
                name: "a.pdf"
            }]
        },
        supporting_documents: [{
            filing_component: "332"
        }],
    });

    assert.strictEqual(bundles[0].filing_component, "332");
});

test("supporting component given as an {id, name} object is flattened to its id", () => {
    const bundles = bundlesFor({
        files: {
            supporting: [{
                name: "a.pdf"
            }]
        },
        supporting_documents: [{
            filing_component: {
                id: "332",
                name: "Attachments"
            }
        }],
    });

    assert.strictEqual(bundles[0].filing_component, "332");
});

test("component stored on the file record is used when the config has none", () => {
    const bundles = bundlesFor({
        files: {
            supporting: [{
                name: "a.pdf",
                filing_component: {
                    id: "332",
                    name: "Attachments"
                }
            }]
        },
        supporting_documents: [{}],
    });

    assert.strictEqual(bundles[0].filing_component, "332");
});

test("a null component falls back instead of throwing on .id", () => {
    // typeof null === "object" -- the previous ternary crashed here.
    const bundles = bundlesFor({
        files: {
            supporting: [{
                name: "a.pdf",
                filing_component: null
            }]
        },
        supporting_documents: [{
            filing_component: null
        }],
    });

    assert.strictEqual(bundles[0].filing_component, CASE_DATA.filing_component);
});

test("with no component anywhere, the case default is used and never the label 'supporting'", () => {
    const bundles = bundlesFor({
        files: {
            supporting: [{
                name: "a.pdf"
            }]
        },
        supporting_documents: [{}],
    });

    assert.strictEqual(bundles[0].filing_component, "999");
    assert.notStrictEqual(bundles[0].filing_component, "supporting");
});

test("lead and supporting documents both land in the bundle", () => {
    const bundles = bundlesFor({
        files: {
            lead: {
                name: "lead.pdf"
            },
            supporting: [{
                name: "a.pdf"
            }]
        },
        lead_filing_component: "331",
        supporting_documents: [{
            filing_component: "332"
        }],
    });

    assert.strictEqual(bundles.length, 2);
    assert.strictEqual(bundles[0].filing_component, "331");
    assert.strictEqual(bundles[1].filing_component, "332");
});

test("the same module object serves both pages, so payloads cannot drift", () => {
    const paymentHandler = makeHandler();
    const reviewHandler = makeHandler();

    assert.strictEqual(paymentHandler.buildEFilingData, reviewHandler.buildEFilingData);
    assert.strictEqual(paymentHandler.addCourtBundles, reviewHandler.addCourtBundles);
    assert.strictEqual(paymentHandler.createDocumentBundle, reviewHandler.createDocumentBundle);
});

test("Cook reference is resolved on the server; DuPage retains its reference", () => {
    const handler = makeHandler();
    const cook = {
        al_court_bundle: []
    };
    const dupage = {
        al_court_bundle: []
    };

    handler.addCourtBundles(cook, {}, {
        ...CASE_DATA,
        court_name: "Cook County"
    }, []);
    handler.addCourtBundles(dupage, {}, {
        ...CASE_DATA,
        court_name: "DuPage County"
    }, []);

    assert.equal(cook.cross_references, undefined);
    assert.deepStrictEqual(dupage.cross_references, {
        136524: "99500"
    });
});

test("saved filer information drives the filing contact instead of account profile data", () => {
    const handler = makeHandler();
    const caseData = {
        filing_parties: [{
            role: "filer",
            first_name: "Jordan",
            last_name: "Taylor",
            address_line_1: "123 Main Street",
            city: "Springfield",
            state: "IL",
            zip_code: "62701",
            email: "jordan@example.com",
            phone: "217-555-0100"
        }]
    };

    assert.deepStrictEqual(handler.userDataFromCaseData(caseData), {
        fullName: "Jordan Taylor",
        address: "123 Main Street",
        addressLine2: "",
        city: "Springfield",
        state: "IL",
        zip: "62701",
        email: "jordan@example.com",
        phone: "217-555-0100"
    });
});

test("amount_in_controversy is sent when the draft has one", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        amount_in_controversy: "12500.00",
        filing_parties: [{
            role: "filer",
            party_type: "PLA",
            first_name: "Jordan",
            last_name: "Taylor"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);

    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.amount_in_controversy, "12500.00");
});

test("defendant aliases are included as the respondent in the filing payload", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "category",
        case_type: "divorce",
        filing_parties: [{
            role: "filer",
            party_type: "PET"
        }],
        defendant_party_type: "DEF",
        defendant_first_name: "Grace",
        defendant_last_name: "Hopper"
    };

    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[1].party_type, "DEF");
    assert.deepStrictEqual(result.users[1].name, {
        first: "Grace",
        middle: "",
        last: "Hopper",
        suffix: ""
    });
});

test("amount_in_controversy is omitted (not sent as empty/zero) when the draft has none", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            party_type: "PLA",
            first_name: "Jordan",
            last_name: "Taylor"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);

    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual("amount_in_controversy" in result, false);
});

test("durable non-filer parties are included without collapsing to one legacy party", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            party_type: "PLA",
            first_name: "Jordan",
            last_name: "Taylor"
        }, {
            role: "other",
            party_type: "DEF",
            first_name: "Alex",
            last_name: "Morgan"
        }, {
            role: "other",
            party_type: "DEF",
            organization_name: "Example LLC"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[0].party_type, "PLA");
    assert.strictEqual(result.other_parties.length, 2);
    assert.strictEqual(result.other_parties[0].name.first, "Alex");
    assert.strictEqual(result.other_parties[1].name.first, "Example LLC");
    assert.strictEqual("address" in result.other_parties[0], false);
    assert.strictEqual("address" in result.other_parties[1], false);
});

test("a saved optional other-party address remains in the filing payload", () => {
    const handler = makeHandler();
    const party = {
        party_type: "DEF",
        first_name: "Alex",
        last_name: "Morgan",
        address_line_1: "10 State Street",
        address_line_2: "Unit 2",
        city: "Chicago",
        state: "IL",
        zip_code: "60601"
    };

    const result = handler.partyFromDraft(party);

    assert.deepStrictEqual(result.address, {
        address: "10 State Street",
        unit: "Unit 2",
        city: "Chicago",
        state: "IL",
        zip: "60601",
        country: "US"
    });
});

test("optional services for lead and supporting documents are included in bundles", () => {
    const bundles = bundlesFor({
        files: {
            lead: {
                name: "main.pdf"
            },
            supporting: [{
                name: "attachment.pdf"
            }]
        },
        lead_optional_services: ["143487"],
        supporting_documents: [{
            filing_component: "332",
            optional_services: ["143491"]
        }]
    });

    assert.strictEqual(bundles.length, 2);
    assert.deepStrictEqual(bundles[0].optional_services, ["143487"]);
    assert.deepStrictEqual(bundles[1].optional_services, ["143491"]);
});
// -- Filing for a party the filer is not ------------------------------------
//
// Being the person filing and being a party are different things. Tyler asks
// only who the filing is on behalf of, so a filer who is not a party names
// someone else and stays out of the caption entirely.

const FILING_FOR_SOMEONE_ELSE = {
    case_category: "cat",
    case_type: "type",
    filing_parties: [{
        role: "filer",
        is_filing_party: false,
        party_type: "",
        first_name: "Helper",
        last_name: "Neighbor",
        email: "helper@example.com"
    }, {
        role: "other",
        is_filing_party: true,
        party_type: "DEF",
        first_name: "Real",
        last_name: "Tenant",
        email: "tenant@example.com"
    }, {
        role: "other",
        is_filing_party: false,
        party_type: "PLA",
        organization_name: "Landlord LLC"
    }]
};

test("a filer who is not a party files on behalf of the party they named", () => {
    const handler = makeHandler();
    const userData = handler.userDataFromCaseData(FILING_FOR_SOMEONE_ELSE);
    const result = handler.buildEFilingData(userData, FILING_FOR_SOMEONE_ELSE, {}, "pay-1");

    assert.strictEqual(result.users.length, 1);
    assert.strictEqual(result.users[0].name.first, "Real");
    assert.strictEqual(result.users[0].party_type, "DEF");
    // The helper reaches the court as the contact, never as a party.
    assert.strictEqual(result.other_parties.length, 1);
    assert.strictEqual(result.other_parties[0].name.first, "Landlord LLC");
    assert.strictEqual(result.lead_contact.name.first, "Helper");
    assert.strictEqual(result.lead_contact.email, "helper@example.com");
});

test("the party being filed for is not repeated in other_parties", () => {
    const handler = makeHandler();
    const userData = handler.userDataFromCaseData(FILING_FOR_SOMEONE_ELSE);
    const result = handler.buildEFilingData(userData, FILING_FOR_SOMEONE_ELSE, {}, "pay-1");

    const names = result.other_parties.map((party) => party.name.first);
    assert.strictEqual(names.includes("Real"), false);
});

test("a filing party with no email of their own borrows the filer's", () => {
    const handler = makeHandler();
    const caseData = structuredClone(FILING_FOR_SOMEONE_ELSE);
    caseData.filing_parties[1].email = "";
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    // Tyler rejects a new case whose first filing party has no email at all.
    assert.strictEqual(result.users[0].email, "helper@example.com");
});

test("every document is filed on behalf of the named party, not the filer", () => {
    const handler = makeHandler();
    const userData = handler.userDataFromCaseData(FILING_FOR_SOMEONE_ELSE);
    const result = handler.buildEFilingData(userData, FILING_FOR_SOMEONE_ELSE, {
        files: {
            lead: {
                name: "answer.pdf"
            }
        }
    }, "pay-1");

    assert.deepStrictEqual(result.al_court_bundle[0].filing_parties, ["users[0]"]);
});

test("a filer who is a party is still the filing party themselves", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            is_filing_party: true,
            party_type: "PLA",
            first_name: "Jordan",
            last_name: "Taylor",
            email: "jordan@example.com"
        }, {
            role: "other",
            is_filing_party: false,
            party_type: "DEF",
            first_name: "Alex",
            last_name: "Morgan"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users.length, 1);
    assert.strictEqual(result.users[0].name.first, "Jordan");
    assert.strictEqual(result.users[0].party_type, "PLA");
    assert.strictEqual(result.other_parties.length, 1);
});

test("co-parties who are both filing are both named as filing parties", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            is_filing_party: false,
            party_type: "",
            first_name: "Helper",
            last_name: "Neighbor",
            email: "helper@example.com"
        }, {
            role: "other",
            is_filing_party: true,
            party_type: "PLA",
            first_name: "First",
            last_name: "Tenant",
            email: "one@example.com"
        }, {
            role: "other",
            is_filing_party: true,
            party_type: "PLA",
            first_name: "Second",
            last_name: "Tenant"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {
        files: {
            lead: {
                name: "complaint.pdf"
            }
        }
    }, "pay-1");

    assert.strictEqual(result.users.length, 2);
    assert.deepStrictEqual(
        result.al_court_bundle[0].filing_parties,
        ["users[0]", "users[1]"]
    );
});
test("the notice address answers both the lead contact and a party with no email", () => {
    const handler = makeHandler();
    const caseData = structuredClone(FILING_FOR_SOMEONE_ELSE);
    caseData.notice_email = "aunt@example.com";
    caseData.filing_parties[1].email = "";
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[0].email, "aunt@example.com");
    assert.strictEqual(result.lead_contact.email, "aunt@example.com");
});

test("a party's own email is not overwritten by the notice address", () => {
    const handler = makeHandler();
    const caseData = structuredClone(FILING_FOR_SOMEONE_ELSE);
    caseData.notice_email = "aunt@example.com";
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[0].email, "tenant@example.com");
});

test("with no notice address given, the filer's own is still what is used", () => {
    const handler = makeHandler();
    const caseData = structuredClone(FILING_FOR_SOMEONE_ELSE);
    caseData.filing_parties[1].email = "";
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[0].email, "helper@example.com");
    assert.strictEqual(result.lead_contact.email, "helper@example.com");
});
// -- Organizations ----------------------------------------------------------

test("an organization says it is a business, so it is not read as a nameless person", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            is_filing_party: true,
            party_type: "PLA",
            first_name: "Quinten",
            last_name: "Steenhuis",
            email: "q@example.com"
        }, {
            role: "other",
            party_type: "DEF",
            organization_name: "Fox River Phone Repair LLC"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    // Without this the court rejects the envelope with "PersonSurName is
    // required", because an organization has no surname to give.
    assert.strictEqual(result.other_parties[0].person_type, "business");
    assert.strictEqual(result.other_parties[0].name.first, "Fox River Phone Repair LLC");
});

test("a person is not labelled a business", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            is_filing_party: true,
            party_type: "PLA",
            first_name: "Quinten",
            last_name: "Steenhuis"
        }, {
            role: "other",
            party_type: "DEF",
            first_name: "Luca",
            last_name: "Martin"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual("person_type" in result.other_parties[0], false);
    assert.strictEqual(result.other_parties[0].name.last, "Martin");
});

test("an organization being filed for is labelled too", () => {
    const handler = makeHandler();
    const caseData = {
        case_category: "cat",
        case_type: "type",
        filing_parties: [{
            role: "filer",
            is_filing_party: false,
            party_type: "",
            first_name: "Quinten",
            last_name: "Steenhuis",
            email: "q@example.com"
        }, {
            role: "other",
            is_filing_party: true,
            party_type: "PLA",
            organization_name: "Riverbend Properties LLC"
        }]
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");

    assert.strictEqual(result.users[0].person_type, "business");
    assert.strictEqual(result.users[0].name.first, "Riverbend Properties LLC");
});
test("identical PDF names and URLs remain separate filings with separate codes", () => {
    const pdf = {
        name: "appearance.pdf",
        url: "https://example.com/appearance.pdf"
    };
    const bundles = bundlesFor({
        files: {
            lead: pdf,
            supporting: [{
                ...pdf
            }]
        },
        lead_filing_type: "appearance",
        supporting_documents: [{
            filing_type: "jury-demand",
            document_type: "public",
            filing_component: "lead"
        }]
    });
    assert.equal(bundles.length, 2);
    assert.deepStrictEqual(bundles.map(bundle => bundle.filename), ["appearance.pdf", "appearance.pdf"]);
    assert.deepStrictEqual(bundles.map(bundle => bundle.filing_type), ["appearance", "jury-demand"]);
});
test("new appeal payload keeps the originating case separate from the appellate case", () => {
    const handler = makeHandler();
    const caseData = {
        existing_case: "new",
        previous_case_id: "",
        filing_parties: [{
            role: "filer",
            party_type: "appellant",
            first_name: "Example",
            last_name: "Filer"
        }],
        lower_court_code: "1753",
        lower_court_prod_code: "6982",
        lower_court_name: "Brighton Division, Boston Municipal Court",
        lower_court_title: "Example v. Test",
        lower_court_docket_number: "24-CV-001",
        lower_court_judge: "Judge Example"
    };
    const userData = handler.userDataFromCaseData(caseData);
    const result = handler.buildEFilingData(userData, caseData, {}, "pay-1");
    assert.deepStrictEqual(result.lower_court_case, {
        title: "Example v. Test",
        docket_number: "24-CV-001",
        judge: "Judge Example"
    });
    assert.deepStrictEqual(result.trial_court, {
        name: caseData.lower_court_name,
        tyler_lower_court_code: "1753",
        tyler_prod_lower_court_code: "6982"
    });
    assert.strictEqual(result.docket_number, undefined);
    assert.strictEqual(result.user_started_case, true);

    caseData.existing_case = "existing";
    caseData.previous_case_id = "court-case-id";
    const existing = handler.buildEFilingData(userData, caseData, {}, "pay-1");
    assert.strictEqual(existing.lower_court_case, undefined);
    assert.strictEqual(existing.trial_court, undefined);
});
test("existing court filing and other parties retain IDs without account duplicates", () => {
    const handler = makeHandler();
    const courtParty = (id, filing) => ({
        id,
        role: "other",
        source: "court",
        external_party_id: `court-${id}`,
        party_type: filing ? "plaintiff" : "defendant",
        first_name: "Court",
        last_name: "Name",
        is_filing_party: filing,
        is_self: filing
    });
    const payload = handler.buildEFilingData({
        fullName: "Account Contact",
        email: "contact@example.com"
    }, {
        previous_case_id: "existing-case",
        court: "kane",
        case_type: "civil",
        filing_parties: [{
                role: "filer",
                first_name: "Account",
                last_name: "Contact"
            },
            courtParty(1, true), courtParty(2, false)
        ]
    }, {
        files: {
            lead: {
                name: "motion.pdf",
                url: "https://example.com/motion.pdf"
            }
        }
    }, "payment");
    assert.equal(payload.users.length, 1);
    assert.equal(payload.other_parties.length, 1);
    assert.equal(payload.users[0].tyler_id, "court-1");
    assert.equal(payload.other_parties[0].tyler_id, "court-2");
    assert.equal(payload.users[0].is_new, false);
    assert.equal(payload.other_parties[0].is_new, false);
    assert.equal(payload.users[0].name.first, "Court");
    assert.deepEqual(payload.al_court_bundle[0].filing_parties, ["users[0]"]);
});

test("an imported party without an ID cannot be serialized as new", () => {
    assert.throws(() => makeHandler().partyFromDraft({
        source: "court",
        party_type: "plaintiff"
    }), /no court ID/);
});

test("explicit new parties have no existing ID", () => {
    const party = makeHandler().partyFromDraft({
        source: "manual",
        first_name: "New",
        last_name: "Person"
    });
    assert.equal(party.is_new, true);
    assert.equal(Object.hasOwn(party, "tyler_id"), false);
});
test("an organization name wins over a stale first name", () => {
    const party = makeHandler().partyFromDraft({
        source: "added",
        id: 7,
        first_name: "Stale",
        organization_name: "Example LLC",
        party_type: "defendant"
    });
    assert.equal(party.person_type, "business");
    assert.equal(party.name.first, "Example LLC");
});