import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ..services.current_drafts import get_current_draft
from ..services.efsp_errors import describe_efsp_error, efsp_error_problems, error_actions
from ..services.efsp_payload import PayloadValidationError, prepare_efile_payload
from ..services.extraction_fields import EXTRACTION_FIELDS, EXTRACTION_HINTS
from ..services.filing_availability import outgoing_unavailable_message
from ..services.submission_errors import SubmissionErrorCode
from ..services.submission_notifications import strip_client_email_fields, submission_email_fields
from ..utils.case_data_utils import get_case_data, get_upload_data, update_case_data
from ..utils.config_loader import config_loader
from ..utils.proxy_connection import get_party_type_code_from_api

logger = logging.getLogger(__name__)

# Backward-compatible names for callers that still import these from this
# legacy session API module. The extraction definitions themselves live with
# the background extraction service now.
llm_hints = EXTRACTION_HINTS
llm_fields = EXTRACTION_FIELDS


def determine_party_type_for_existing_case(case_data):
    """
    Determine the appropriate party type when responding to an existing case.
    This fetches actual party type codes from the API.
    """
    court_code = case_data.get("court")
    case_type_code = case_data.get("case_type")
    case_type = case_data.get("case_type", "").lower()
    filing_type = case_data.get("filing_type", "").lower()
    jurisdiction = case_data.get("jurisdiction")

    logger.debug("Determining party type for existing case:")
    logger.debug(f"  Court: {court_code}")
    logger.debug(f"  Case type code: {case_type_code}")
    logger.debug(f"  Case type: {case_type}")
    logger.debug(f"  Filing type: {filing_type}")

    if not court_code or not case_type_code:
        logger.warning("Missing court or case_type for party type determination")
        return "DEF"  # Default fallback code

    # Determine target party type name based on case and filing type
    target_party_name = None

    if "criminal" in case_type:
        target_party_name = "defendant"
    elif "civil" in case_type or "family" in case_type:
        if "answer" in filing_type or "response" in filing_type:
            target_party_name = "respondent"
        else:
            target_party_name = "defendant"
    elif "probate" in case_type:
        target_party_name = "interested party"
    else:
        target_party_name = "defendant"

    logger.debug(f"Determined target party name: {target_party_name}")

    # Get the actual party type code from API
    party_code = get_party_type_code_from_api(
        court_code, case_type_code, jurisdiction, target_party_name=target_party_name
    )

    logger.debug(f"API returned party code: {party_code}")

    # Fallback codes if API call fails
    if not party_code:
        logger.warning("Using fallback codes since API call failed")
        if target_party_name == "defendant":
            fallback = "DEF"
        elif target_party_name == "respondent":
            fallback = "RES"
        elif target_party_name == "interested party":
            fallback = "INT"
        else:
            fallback = "DEF"
        logger.info(f"Using fallback party type code: {fallback}")
        return fallback

    logger.info(f"Selected party type code: {party_code}")
    return party_code


@require_http_methods(["GET"])
def get_upload_data_from_session(request):
    """Return the current draft's documents as the upload_data blob."""
    return JsonResponse(get_upload_data(request), safe=False)


def forward_final_filing(request, data):
    """Send a confirmed filing to the court.

    Only views/submission.py calls this, after it has parsed the body and
    checked the filer's confirmation and acceptance of the court requirements.
    """
    try:
        # Read the filing state from the current durable draft
        case_data = get_case_data(request)
        jurisdiction_id = request.session.get("jurisdiction") or case_data.get("jurisdiction")
        upload_data = get_upload_data(request)

        if not case_data:
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.CASE_DATA_MISSING,
                    "error": "No case data found in session. Please go back and resubmit your case information.",
                    "debug_info": "Session case_data is empty",
                },
                status=400,
            )

        if not upload_data or not upload_data.get("files"):
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.UPLOAD_DATA_MISSING,
                    "error": "No upload data found in session. Please go back and resubmit your documents.",
                    "debug_info": f"Upload data: {upload_data}",
                },
                status=400,
            )

        # Extract efile_data from the request
        efile_data = data.get("efile_data", {})
        if not efile_data:
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.EFILE_DATA_MISSING,
                    "error": "No efile data provided in request",
                },
                status=400,
            )

        # Notification copy is server-owned. Ignore all browser-supplied values
        # even while the jurisdiction's email feature flag is disabled.
        if isinstance(efile_data, dict):
            strip_client_email_fields(efile_data)

        # Validate required fields in efile_data
        required_fields = ["al_court_bundle"]  # Based on typical Suffolk API requirements
        missing_fields = [field for field in required_fields if field not in efile_data]
        if missing_fields:
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.EFILE_DATA_INVALID,
                    "error": f"Missing required fields in efile_data: {missing_fields}",
                },
                status=400,
            )

        # Get jurisdiction and court info from case data
        court_id = case_data.get("court", "")

        if not court_id:
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.COURT_ID_MISSING,
                    "error": "Court ID is required for filing submission",
                },
                status=400,
            )

        draft = get_current_draft(request, jurisdiction=jurisdiction_id, resume_latest=False)

        # Same fixups the fee quote applied, so the filing matches the quote.
        try:
            prepare_efile_payload(efile_data, jurisdiction_id, court_id, draft=draft)
        except PayloadValidationError as error:
            return JsonResponse(
                {
                    "success": False,
                    "error_code": SubmissionErrorCode.PAYLOAD_VALIDATION_FAILED,
                    "error_actions": error_actions(draft, efile_data, error.problems),
                    "error": str(error),
                },
                status=400,
            )

        # Resolve the outgoing IDs to authoritative names before applying rules.
        try:
            message, status = outgoing_unavailable_message(jurisdiction_id, court_id, case_data, efile_data), 403
        except ValueError as error:
            message, status = str(error), 412
        if message:
            return JsonResponse(
                {"success": False, "error_code": SubmissionErrorCode.FILING_UNAVAILABLE, "error": message},
                status=status,
            )

        try:
            jurisdiction_config = config_loader.load_jurisdiction_config(jurisdiction_id)
            efile_data.update(submission_email_fields(request, draft, jurisdiction_id, jurisdiction_config))
        except Exception:
            # Email copy is optional. A missing config or bad public host must
            # never prevent the filing from reaching the court.
            logger.exception("Could not prepare submission email copy for %s", jurisdiction_id)

        # Construct the Suffolk LIT Lab API endpoint
        api_url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction_id}/filingreview/courts/{court_id}/filings"

        # Make the API call to Suffolk LIT Lab
        import requests

        try:
            # Get API key from Django settings
            api_key = getattr(settings, "SUFFOLK_EFILE_API_KEY", "")

            # Get Tyler token from session (similar to auth_views.py)
            auth_tokens = request.session.get("auth_tokens", {})
            tyler_token = (
                auth_tokens.get(f"TYLER-TOKEN-{jurisdiction_id.upper()}")
                or auth_tokens.get(f"tyler_token_{jurisdiction_id}")
                or auth_tokens.get(f"tyler-token-{jurisdiction_id}")
            )

            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"{jurisdiction_id.title()}-eFile-Client/1.0",
            }

            if api_key:
                headers["X-API-Key"] = api_key

            # Add Tyler token if available (following auth_views.py pattern)
            if tyler_token:
                headers[f"TYLER-TOKEN-{jurisdiction_id.upper()}"] = tyler_token
            else:
                logger.warning(f"No Tyler token found for jurisdiction '{jurisdiction_id}' in filing submission")

            logger.info(f"Submitting to Suffolk LIT Lab API at: {api_url}")

            response = requests.post(
                api_url,
                json=efile_data,
                headers=headers,
                allow_redirects=False,
                timeout=(
                    settings.EFILE_SUBMISSION_CONNECT_TIMEOUT_SECONDS,
                    settings.EFILE_SUBMISSION_READ_TIMEOUT_SECONDS,
                ),
            )

            logger.info("Filing submission response status=%s", response.status_code)

            if response.status_code == 200 or response.status_code == 201:
                response_data = response.json()

                # Draft status (mark_submitted / clear) is handled by the
                # submission wrapper in views/submission.py that calls this view.
                return JsonResponse(
                    {
                        "success": True,
                        "message": "Filing submitted successfully",
                        "redirect_url": f"/jurisdiction/{jurisdiction_id}/filing-confirmation/",
                        "api_response": response_data,
                    }
                )
            else:
                # Handle API error responses
                logger.error(f"API Error - Status: {response.status_code}")

                error_message = describe_efsp_error(response)

                return JsonResponse(
                    {
                        "success": False,
                        "error": f"Filing submission failed: {error_message}",
                        "error_actions": error_actions(
                            draft, efile_data, efsp_error_problems(response), message=error_message
                        ),
                        "api_status_code": response.status_code,
                        "api_response": response.text[:500] if response.text else "No response body",
                    },
                    status=response.status_code,
                )

        except requests.RequestException as e:
            return JsonResponse(
                {"success": False, "error": f"Network error during filing submission: {str(e)}"}, status=500
            )

    except Exception as e:
        return JsonResponse({"success": False, "error": f"An error occurred during submission: {str(e)}"}, status=500)


@require_http_methods(["POST"])
def clear_session_data(request):
    """Clear all session data for testing purposes."""
    request.session.flush()
    return JsonResponse({"success": True, "message": "Session data cleared"})


@require_http_methods(["POST"])
def api_save_case_data(request):
    """
    API endpoint to save case data to session
    """
    try:
        if not request.user.is_authenticated:
            return JsonResponse({"success": False, "error": "Authentication required"}, status=401)

        data = json.loads(request.body)

        session_id = data.get("session_id")
        if session_id:
            request.session["session_id"] = session_id

        jurisdiction = data.get("jurisdiction")
        if jurisdiction:
            request.session["jurisdiction"] = jurisdiction

        # Handle two different data structures:
        # 1. From form-validation.js: { data: { form_fields... } }
        # 2. From existing cases: { existing_case: 'yes', case_tracking_id: '...', ... }

        if "data" in data:
            # Structure from form-validation.js (expert form)
            form_data = data.get("data", {})
            existing_case = form_data.get("existing_case")
        else:
            # Structure from existing cases (direct fields)
            form_data = data
            existing_case = data.get("existing_case")

        updates = dict(form_data)
        if existing_case:
            request.session["existing_case"] = existing_case
            updates["existing_case"] = existing_case

        # Determine party type for existing cases - typically defendant/respondent when responding
        if existing_case == "yes" and not updates.get("party_type"):
            party_type = determine_party_type_for_existing_case(form_data)
            updates["party_type"] = party_type
            updates.setdefault("petitioner_party_type", party_type)

        update_case_data(request, updates, jurisdiction)

        return JsonResponse(
            {"success": True, "data": {"existing_case": existing_case, "saved_fields": list(form_data.keys())}}
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logging.exception("Error when trying to save case data")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def fetch_and_save_party_type(request):
    """
    Fetch party type code from Suffolk LIT Lab API and save to session
    """
    try:
        if not request.user.is_authenticated:
            return JsonResponse({"success": False, "error": "Authentication required"}, status=401)

        data = json.loads(request.body)
        court_code = data.get("court")
        case_type_code = data.get("case_type")
        existing_case = data.get("existing_case")
        jurisdiction = data.get("jurisdiction")

        logger.debug("Fetching party type with data")

        if not jurisdiction:
            return JsonResponse({"success": False, "error": "Jurisdiction is required"}, status=400)

        request.session["jurisdiction"] = jurisdiction

        if not court_code or not case_type_code:
            return JsonResponse({"success": False, "error": "Court and case_type are required"}, status=400)

        # Determine target party type based on existing case status
        if existing_case == "yes":
            logger.debug("Determining party type for existing case")
            party_type_code = determine_party_type_for_existing_case(
                {
                    "court": court_code,
                    "case_type": case_type_code,
                    "filing_type": data.get("filing_type", ""),
                    "existing_case": existing_case,
                }
            )
        else:
            logger.debug("Determining party type for new case")
            # For new cases, determine appropriate party type
            case_type = case_type_code.lower()
            logger.debug(f"Case type (lowercase): {case_type}")

            if "name change" in case_type or "family" in case_type or "probate" in case_type:
                logger.debug("Looking for petitioner party type")
                party_type_code = get_party_type_code_from_api(
                    court_code, case_type_code, jurisdiction, target_party_name="petitioner"
                )
            elif "civil" in case_type:
                logger.debug("Looking for plaintiff party type")
                party_type_code = get_party_type_code_from_api(
                    court_code, case_type_code, jurisdiction, target_party_name="plaintiff"
                )
            else:
                logger.debug("Default: Looking for petitioner party type")
                party_type_code = get_party_type_code_from_api(
                    court_code, case_type_code, jurisdiction, target_party_name="petitioner"
                )

            # If API call failed, return error instead of using fallback codes
            if not party_type_code:
                logger.warning("API call failed, no party type could be determined from Suffolk API")
                return JsonResponse(
                    {
                        "success": False,
                        "error": f"Unable to determine party type from Suffolk API for court '{court_code}'"
                        f" and case type '{case_type_code}'",
                    },
                    status=400,
                )

        logger.debug(f"Final party type code: {party_type_code}")

        update_case_data(
            request,
            {
                "determined_party_type": party_type_code,
                "party_type": party_type_code,
                "petitioner_party_type": party_type_code,
            },
            jurisdiction,
        )

        logger.debug(f"Saved party type to draft: {party_type_code}")

        return JsonResponse({"success": True, "party_type": party_type_code})

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def save_party_type_to_session(request):
    """
    Save party type code to session after it's been fetched from Suffolk API on frontend
    """
    try:
        if not request.user.is_authenticated:
            return JsonResponse({"success": False, "error": "Authentication required"}, status=401)

        data = json.loads(request.body)
        party_type = data.get("party_type")

        if not party_type:
            return JsonResponse({"success": False, "error": "Party type is required"}, status=400)

        update_case_data(
            request,
            {
                "determined_party_type": party_type,
                "party_type": party_type,
                "petitioner_party_type": party_type,
            },
        )

        logger.debug(f"Saved party type to draft: {party_type}")

        return JsonResponse({"success": True, "party_type": party_type})

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
