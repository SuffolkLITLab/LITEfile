"""
API views for Suffolk LIT Lab integration
"""

import json
import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from requests.exceptions import RequestException

from efile.utils.case_data_utils import get_case_data, update_case_data
from efile.utils.efsp_security import validated_code, validated_jurisdiction
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request, get_jurisdiction_token, has_jurisdiction_login
from efile.utils.proxy_connection import get_headers

logger = logging.getLogger(__name__)


def get_tyler_token(request, jurisdiction=None):
    """Helper method to retrieve Tyler token from various sources"""
    if jurisdiction is None:
        jurisdiction = get_jurisdiction_from_request(request)

    return get_jurisdiction_token(request, jurisdiction)


def set_access_control_headers(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken"
    return response


@require_http_methods(["GET", "POST", "OPTIONS"])
def lookup_case(request):
    """
    Look up a case using Suffolk LIT Lab API

    For GET requests, expects query parameters: court and caseNumber
    For POST requests, expects JSON body with: court and caseNumber

    Returns case information including caseTrackingID and caseCategoryText
    """

    # Handle preflight OPTIONS request
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return set_access_control_headers(response)

    try:
        # Get parameters based on request method
        if request.method == "GET":
            court = request.GET.get("court")
            case_number = request.GET.get("caseNumber")
            jurisdiction = request.GET.get("jurisdiction")
        else:  # POST
            data = json.loads(request.body)
            court = data.get("court")
            case_number = data.get("caseNumber")
            jurisdiction = data.get("jurisdiction")

        if not court or not case_number:
            response = JsonResponse({"success": False, "error": "Both court and caseNumber are required"}, status=400)
            return set_access_control_headers(response)

        # Suffolk eFile API endpoint - build URL with court and case number
        # Use provided jurisdiction or fall back to request-based detection
        if jurisdiction:
            state = jurisdiction.lower()
        else:
            state = get_jurisdiction_from_request(request)
        state = validated_jurisdiction(state)
        court = validated_code(court)
        if not has_jurisdiction_login(request, state):
            return JsonResponse({"success": False, "error": "Sign in to this jurisdiction first."}, status=401)
        api_url = f"{settings.EFSP_URL}/jurisdictions/{state}/cases/courts/{court}/cases"

        # Add query parameter for docket number
        params = {"docket_number": case_number}

        logger.info(f"Looking up case {case_number} in court {court} at {api_url}")

        # Get authentication credentials dynamically
        tyler_token = get_tyler_token(request, state)

        headers = get_headers()
        # Add Tyler token if available
        if tyler_token:
            headers[f"tyler-token-{state}"] = tyler_token
        else:
            # Log that no token was found for debugging
            logger.info(
                "No Tyler token found for state '%s' in Suffolk case lookup request",
                state,
            )

        # Make the API request - using GET with query parameters
        response = requests.get(api_url, params=params, headers=headers, timeout=30, allow_redirects=False)

        response.raise_for_status()
        api_data = response.json()

        # Extract relevant information from the eFile API response
        # The response should be an array of case objects
        case_info = {}

        if api_data and len(api_data) > 0:
            # Take the first case from the response
            case_data = api_data[0]

            # Extract case information from the eFile API response structure
            if "value" in case_data:
                case_value = case_data["value"]

                # Extract caseTrackingID
                if "caseTrackingID" in case_value and "value" in case_value["caseTrackingID"]:
                    case_info["caseTrackingID"] = case_value["caseTrackingID"]["value"]

                # Extract case category
                if "caseCategoryText" in case_value and "value" in case_value["caseCategoryText"]:
                    case_info["caseCategoryCode"] = case_value["caseCategoryText"]["value"]
                    api_url = f"{settings.EFSP_URL}/jurisdictions/{state}/codes/courts/{court}/categories/{validated_code(case_info['caseCategoryCode'])}"
                    category_resp = requests.get(api_url, headers=headers, timeout=30, allow_redirects=False)
                    if category_resp.ok:
                        data = category_resp.json()
                        case_info["caseCategoryName"] = data["name"]

                # Extract case title
                if "caseTitleText" in case_value and "value" in case_value["caseTitleText"]:
                    case_info["caseTitle"] = case_value["caseTitleText"]["value"]

                # Extract case docket ID
                if "caseDocketID" in case_value and "value" in case_value["caseDocketID"]:
                    case_info["caseDocketID"] = case_value["caseDocketID"]["value"]
                else:
                    case_info["caseDocketID"] = case_number

                # Extract case type from extensions if available
                if "rest" in case_value:
                    for item in case_value["rest"]:
                        if item.get("name") == "{urn:tyler:ecf:extensions:Common}CaseAugmentation":
                            if "value" in item and "caseTypeText" in item["value"]:
                                if "value" in item["value"]["caseTypeText"]:
                                    case_info["caseTypeCode"] = item["value"]["caseTypeText"]["value"]
                                    api_url = f"{settings.EFSP_URL}/jurisdictions/{state}/codes/courts/{court}/case_types/{validated_code(case_info['caseTypeCode'])}"
                                    case_resp = requests.get(
                                        api_url, headers=headers, timeout=30, allow_redirects=False
                                    )
                                    if case_resp.ok:
                                        data = case_resp.json()
                                        case_info["caseTypeName"] = data["name"]
                                    break
            else:
                # If no 'value' key, try to extract directly
                case_info = {
                    "caseTrackingID": case_data.get("caseTrackingID", ""),
                    "caseCategoryText": case_data.get("caseCategoryText", ""),
                    "caseTitle": case_data.get("caseTitleText", ""),
                    "caseDocketID": case_data.get("caseDocketID", case_number),
                    "caseTypeText": case_data.get("caseTypeText", ""),
                }

        success = True
        if not case_info.get("caseTrackingID") and not case_info.get("caseCategoryText"):
            logger.warning("Could not extract case information from API response")
            case_info = {}
            success = False

        response = JsonResponse(
            {
                "success": success,
                "caseInfo": case_info,
                "fullResponse": api_data,  # Include full response for debugging
            }
        )

        return set_access_control_headers(response)

    except (ValueError, UnicodeDecodeError):
        # This only applies to POST requests
        response = JsonResponse({"success": False, "error": "Invalid jurisdiction, court, or request data"}, status=400)
        return set_access_control_headers(response)

    except RequestException as e:
        logger.error(f"Suffolk API request failed: {str(e)}")
        response = JsonResponse(
            {"success": False, "error": "Failed to connect to Suffolk API", "details": str(e)}, status=500
        )
        return set_access_control_headers(response)

    except Exception as e:
        logger.error(f"Unexpected error in case lookup: {str(e)}")
        response = JsonResponse(
            {"success": False, "error": "An unexpected error occurred", "details": str(e)}, status=500
        )
        return set_access_control_headers(response)


@require_http_methods(["GET"])
def get_party_types_from_suffolk_api(request):
    """
    Fetch party types directly from Suffolk API and save to session (GET request)
    """
    try:
        jurisdiction = request.GET.get("jurisdiction")
        court = request.GET.get("court")
        case_type = request.GET.get("case_type")
        existing_case = request.GET.get("existing_case", "no")
        only_required = request.GET.get("only_required", "False").lower() == "true"

        if not court or not case_type:
            return JsonResponse({"success": False, "error": "Court and case_type parameters are required"}, status=400)

        # Construct Suffolk API URL
        suffolk_api_url = (
            f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{court}/case_types/{case_type}/party_types"
        )

        logger.debug(f"Fetching party types from Suffolk API: {suffolk_api_url}")
        logger.debug(f"Existing case: {existing_case}")

        # Make request to Suffolk API
        response = requests.get(suffolk_api_url, timeout=10)

        if response.status_code == 200:
            party_types = response.json()
            logger.debug(f"Suffolk API returned {len(party_types)} party types:")
            for pt in party_types:
                logger.debug(f"  - {pt.get('name', 'No name')} ({pt.get('code', 'No code')})")

            if party_types and len(party_types) > 0:
                # Determine appropriate party type based on case status
                selected_party_type = None

                if existing_case == "yes":
                    # For existing cases, look for defendant party type
                    logger.debug("Looking for defendant party type for existing case")
                    for party_type in party_types:
                        if isinstance(party_type, dict) and "name" in party_type and "code" in party_type:
                            party_name_lower = party_type["name"].lower()
                            if (
                                "defendant" in party_name_lower
                                or "respondent" in party_name_lower
                                or "def" in party_name_lower
                            ):
                                selected_party_type = party_type["code"]
                                logger.info(
                                    f"Found defendant/respondent party type: "
                                    f"{party_type['name']} ({selected_party_type})"
                                )
                                break
                else:
                    # For new cases, look for petitioner or plaintiff party type
                    logger.debug("Looking for petitioner/plaintiff party type for new case")
                    case_type_lower = case_type.lower()

                    target_names = []
                    if "name change" in case_type_lower or "family" in case_type_lower or "probate" in case_type_lower:
                        target_names = ["petitioner", "pet"]
                    elif "civil" in case_type_lower:
                        target_names = ["plaintiff", "pl"]
                    else:
                        target_names = ["petitioner", "pet", "plaintiff", "pl"]

                    for target_name in target_names:
                        for party_type in party_types:
                            if isinstance(party_type, dict) and "name" in party_type and "code" in party_type:
                                party_name_lower = party_type["name"].lower()
                                if target_name in party_name_lower:
                                    selected_party_type = party_type["code"]
                                    logger.info(
                                        f"Found {target_name} party type: {party_type['name']} ({selected_party_type})"
                                    )
                                    break
                        if selected_party_type:
                            break

                # If no specific match found, use the first available party type
                if not selected_party_type:
                    selected_party_type = party_types[0].get("code")
                    logger.info(
                        f"No specific match found, using first party type: "
                        f"{party_types[0].get('name', 'Unknown')} ({selected_party_type})"
                    )

                # Seed the draft's party type, but never overwrite one it already
                # has. Everything above is a *guess* from the case type's name
                # ("civil" -> plaintiff), and this endpoint is a GET called on
                # page load -- including from review, long after the filer chose.
                # Overwriting there silently replaced a filer who had picked
                # Defendant/Respondent with the guessed Plaintiff/Petitioner,
                # leaving both sides the same party type and the filing rejected
                # by the court for a required party that no longer had anyone in
                # it. The guess is a default for an empty draft, not an answer.
                stored_case_data = get_case_data(request, jurisdiction) or {}
                stored_party_type = next(
                    (
                        stored_case_data.get(key)
                        for key in ("determined_party_type", "party_type", "petitioner_party_type")
                        if stored_case_data.get(key)
                    ),
                    "",
                )

                if stored_party_type:
                    # Answer with what the draft holds, so the page cannot show a
                    # different party type from the one that will be filed.
                    selected_party_type = stored_party_type
                    update_case_data(request, {"existing_case": existing_case}, jurisdiction)
                    logger.debug("Kept the party type already on the draft: %s", stored_party_type)
                else:
                    update_case_data(
                        request,
                        {
                            "determined_party_type": selected_party_type,
                            "party_type": selected_party_type,
                            "petitioner_party_type": selected_party_type,
                            "existing_case": existing_case,
                        },
                        jurisdiction,
                    )
                    logger.debug(f"Saved party type to draft: {selected_party_type}")

                if only_required:
                    filtered_party_types = [p for p in party_types if p.get("isrequired", False)]
                else:
                    filtered_party_types = party_types
                logger.info("only_required: %s, filtered: %s", only_required, filtered_party_types)
                return JsonResponse(
                    {"success": True, "party_types": filtered_party_types, "selected_party_type": selected_party_type}
                )
            else:
                return JsonResponse({"success": False, "error": "No party types returned from Suffolk API"}, status=400)
        else:
            logger.warning(f"Suffolk API request failed with status: {response.status_code}")
            return JsonResponse(
                {"success": False, "error": f"Suffolk API returned status {response.status_code}"},
                status=response.status_code,
            )

    except requests.RequestException as e:
        logger.warning(f"Network error calling Suffolk API: {e}")
        return JsonResponse({"success": False, "error": f"Network error: {str(e)}"}, status=500)
    except Exception as e:
        logger.warning(f"Unexpected error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)
