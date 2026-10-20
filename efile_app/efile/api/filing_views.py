"""
API views for filing operations and document management
"""

import json
import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from efile.utils.efsp_security import require_efsp_login, validated_code, validated_jurisdiction
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request

from ..services.court_payments import payment_error, validate_filing_payment
from ..services.current_drafts import get_current_draft
from ..services.efsp_errors import describe_efsp_error, efsp_error_problems, error_actions
from ..services.efsp_payload import PayloadValidationError, prepare_efile_payload
from ..services.fee_quotes import fee_inputs_token, fee_quote_summary, quote_from_efsp_response, record_fee_quote
from ..utils.case_data_utils import get_case_data
from ..utils.proxy_connection import get_headers
from .base import APIResponseMixin

logger = logging.getLogger(__name__)


def get_tyler_token(request, jurisdiction=None):
    """Helper method to retrieve Tyler token from various sources"""
    if jurisdiction is None:
        jurisdiction = get_jurisdiction_from_request(request)

    # Fallback to session
    auth_tokens = request.session.get("auth_tokens", {})

    # Try different Tyler token key formats
    tyler_token = (
        auth_tokens.get(f"TYLER-TOKEN-{jurisdiction.upper()}")
        or auth_tokens.get(f"tyler_token_{jurisdiction}")
        or auth_tokens.get(f"tyler-token-{jurisdiction}")
    )

    return tyler_token


def list_filing_data(request, jurisdiction, *, start_date=None):
    """Return the current Tyler account's filings in its normalized API shape.

    Both the filing-history endpoint and the plan case-link action need this
    account-scoped list. Keeping the proxy call here means the latter cannot
    accidentally validate a browser-supplied case ID instead.
    """

    jurisdiction = validated_jurisdiction(jurisdiction)
    api_url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/filingreview/courts/0/filings"
    headers = get_headers()
    tyler_token = get_tyler_token(request, jurisdiction)
    if tyler_token:
        headers[f"tyler-token-{jurisdiction}"] = tyler_token
    else:
        logger.info("No Tyler token found for state '%s' in filing-history request", jurisdiction)

    response = requests.get(
        api_url,
        params={"start_date": start_date or None, "before_date": None},
        headers=headers,
        timeout=30,
        allow_redirects=False,
    )
    logger.debug(
        "Get filings response: status=%s content_type=%s",
        response.status_code,
        response.headers.get("Content-Type"),
    )
    response.raise_for_status()
    return [FilingAPIViews.convert_filing_data(filing) for filing in response.json()]


def accepted_case_for_user(request, jurisdiction, case_tracking_id, *, start_date=None):
    """Return an accepted case the current account has filed into, if any."""

    wanted = str(case_tracking_id or "")
    if not wanted:
        return None
    return next(
        (
            filing
            for filing in list_filing_data(request, jurisdiction, start_date=start_date)
            if str(filing.get("filing_status", "")).lower() == "accepted"
            and str(filing.get("case_tracking_id", "")) == wanted
            and filing.get("case_number")
        ),
        None,
    )


class FilingAPIViews(APIResponseMixin):
    """API views for filing operations"""

    @staticmethod
    @require_http_methods(["GET"])
    @require_efsp_login
    def get_filings(request):
        try:
            # Get all filings in all courts between now and a specified time
            jurisdiction = get_jurisdiction_from_request(request)
            start_date = request.GET.get("start_date")

            if not jurisdiction:
                return JsonResponse({"success": False, "error": "Jurisdiction parameter is required"}, status=400)

            return FilingAPIViews.success_response(list_filing_data(request, jurisdiction, start_date=start_date))
        except requests.RequestException as e:
            logger.exception("Network error calling Suffolk API")
            return FilingAPIViews.error_response(f"Network error: {str(e)}", status_code=500)
        except Exception as e:
            logger.exception("Unexpected error")
            return FilingAPIViews.error_response(f"Error: {str(e)}", status_code=500)

    @staticmethod
    def convert_filing_data(filing):
        data = {}
        data["filing_status"] = filing.get("filingStatus").get("filingStatusCode")
        # Tyler identifies the same filing two ways: the envelope number a clerk
        # quotes over the phone, and the FILINGID the detail endpoint wants. The
        # detail endpoint answers 422 to an envelope number, so keep both.
        for identifier in filing.get("documentIdentification") or []:
            category = ((identifier.get("identificationCategory") or {}).get("value") or {}).get("value", "")
            value = (identifier.get("identificationID") or {}).get("value", "")
            if str(category).upper() == "FILINGID":
                data["filing_id"] = value
            elif str(category).upper() == "ENVELOPEID":
                data["envelope_id"] = value
        data["filing_status_text"] = next(iter(filing.get("filingStatus").get("statusDescriptionText"))).get("value")
        data["case_tracking_id"] = filing.get("caseTrackingID").get("value")
        data["filed_timestamp"] = filing.get("documentFiledDate").get("dateRepresentation").get("value").get("value")
        data["received_timestamp"] = (
            filing.get("documentReceivedDate").get("dateRepresentation").get("value").get("value")
        )
        for category in filing["documentCategoryText"]:
            if category["name"] == "{urn:tyler:ecf:extensions:Common}CaseTitle":
                data["case_title"] = category.get("value").get("value")
            if category["name"] == "{urn:tyler:ecf:extensions:Common}CaseNumber":
                data["case_number"] = category.get("value").get("value")
            if category["name"] == "{urn:tyler:ecf:extensions:Common}OrganizationIdentificationID":
                data["court_code"] = category.get("value").get("value")
            if category["name"] == "{urn:tyler:ecf:extensions:Common}FilingCode":
                data["filing_code"] = category.get("value").get("value")
        # TODO(brycew): submitted by?
        return data

    @staticmethod
    @require_http_methods(["POST"])
    @require_efsp_login(session_jurisdiction=True)
    def payment_fees(request):
        try:
            logger.info("Test in payment fees!")
            data = json.loads(request.body)
            efile_data = data.get("efile_data", {})
            if not efile_data:
                return JsonResponse({"success": False, "error": "No efile data provided in request"}, status=400)

            jurisdiction_id = validated_jurisdiction(request.session.get("jurisdiction"))
            auth_tokens = request.session.get("auth_tokens", {})
            case_data = get_case_data(request, jurisdiction_id)
            court_id = case_data.get("court", "")

            draft = get_current_draft(request, jurisdiction=jurisdiction_id, resume_latest=False)
            if draft is not None:
                court_id = draft.court_code
            court_id = validated_code(court_id)
            account_id = str(data.get("payment_account_id") or (draft.selected_payment_account_id if draft else ""))
            try:
                validate_filing_payment(
                    jurisdiction_id, court_id, get_tyler_token(request, jurisdiction_id), account_id
                )
            except ValueError as error:
                return JsonResponse(payment_error(draft, error), status=412)
            # Price the account we checked, never a different ID in the browser payload.
            efile_data["tyler_payment_id"] = account_id

            # Must match what submit_final_filing sends, or fees are quoted
            # against a payload that differs from the one actually filed.
            try:
                prepare_efile_payload(efile_data, jurisdiction_id, court_id, draft=draft)
            except PayloadValidationError as error:
                # Known-bad payload: answer with the specific reason rather than
                # letting the EFSP reply with a code-list error no filer can act on.
                return JsonResponse(
                    {
                        "success": False,
                        "error": str(error),
                        "error_actions": error_actions(draft, efile_data, error.problems),
                    },
                    status=400,
                )

            url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction_id}/filingreview/courts/{court_id}/filing/fees"

            headers = get_headers()
            tyler_token = (
                auth_tokens.get(f"TYLER-TOKEN-{jurisdiction_id.upper()}")
                or auth_tokens.get(f"tyler_token_{jurisdiction_id}")
                or auth_tokens.get(f"tyler-token-{jurisdiction_id}")
            )

            # Add Tyler token if available (following auth_views.py pattern)
            if tyler_token:
                headers[f"TYLER-TOKEN-{jurisdiction_id.upper()}"] = tyler_token
            else:
                logger.warning(f"No Tyler token found for jurisdiction '{jurisdiction_id}' in filing submission")

            # The draft this request prices, as the page that built it saw it.
            # Checked again when the EFSP answers: the request can take a
            # minute, and the filing can be edited in another tab meanwhile.
            inputs_token = str(data.get("fee_inputs_token") or "")
            priced_current_draft = draft is not None and bool(inputs_token) and fee_inputs_token(draft) == inputs_token

            logger.info(f"Making request!: {url}")
            response = requests.post(url, json=efile_data, headers=headers, timeout=60, allow_redirects=False)
            logger.info(f"Made request: {response.status_code}")

            if response.status_code == 200 or response.status_code == 201:
                response_data = response.json()

                logger.debug("Fee calculation succeeded")
                # Kept on the draft with what it was priced on, so Review shows
                # it only while it still describes the filing. A quote for a
                # version of the filing that has since changed is not kept.
                quote = quote_from_efsp_response(response_data)
                recorded = False
                if draft is not None and quote is not None and priced_current_draft:
                    total, breakdown = quote
                    recorded = record_fee_quote(
                        draft,
                        total,
                        breakdown,
                        payment_account_id=account_id,
                        inputs_token=inputs_token,
                    )
                return JsonResponse(
                    {
                        "success": True,
                        "message": "Payment fees submitted successfully",
                        "api_response": response_data,
                        "quote_recorded": recorded,
                        # The filing changed since the page priced it: ask again
                        # from a page that shows the current filing.
                        "quote_superseded": quote is not None and not recorded and draft is not None,
                        "quote": fee_quote_summary(draft) if draft is not None else None,
                    }
                )
            else:
                logger.warning("EFSP fee request failed status=%s", response.status_code)
                error_message = describe_efsp_error(response)

                return JsonResponse(
                    {
                        "success": False,
                        "error": f"Could not get filing fees: {error_message}",
                        "error_actions": error_actions(
                            draft, efile_data, efsp_error_problems(response), message=error_message
                        ),
                        "api_status_code": response.status_code,
                        "api_response": response.text[:500] if response.text else "No response body",
                    },
                    status=response.status_code,
                )

        except json.JSONDecodeError:
            return FilingAPIViews.error_response("Invalid JSON data")
        except Exception as e:
            return FilingAPIViews.error_response(f"Error: {str(e)}")


# Individual view functions for URL mapping
get_filings = FilingAPIViews.get_filings
payment_fees = FilingAPIViews.payment_fees
