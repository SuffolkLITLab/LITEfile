"""
API views for dropdown data in Illinois eFile system
Handles cascading dropdowns for case categories, types, counties, etc.
Uses GET requests to external APIs exclusively.
"""

import logging
import re

import requests
from django.conf import settings
from django.views.decorators.http import require_http_methods

from efile.services.court_selection import heading_court_codes, is_non_filing_court
from efile.services.efsp_payload import parse_optional_services
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request

from ..utils.zip_to_county_il import get_county_by_zip
from .base import APIResponseMixin

logger = logging.getLogger(__name__)

# Words that appear in nearly every Illinois court name, so they say nothing
# about which county a guess is pointing at.
_COURT_NOISE_WORDS = frozenset(
    {"circuit", "county", "court", "courts", "division", "illinois", "in", "judicial", "of", "the"}
)


def _county_tokens(text):
    """Split a court name into the words that could name a county."""
    words = re.split(r"[^a-z0-9]+", str(text or "").lower())
    return [word for word in words if word and word not in _COURT_NOISE_WORDS]


def _county_keys(text):
    """Every run of whole words in a court name, joined the way court codes are.

    Court codes drop the spaces inside a county name ("stclair", "rockisland"),
    so a run of whole words is the smallest unit worth comparing. Comparing runs
    instead of raw substrings is what keeps "Henry County" from matching a
    document that said "McHenry County".
    """
    tokens = _county_tokens(text)
    return {"".join(tokens[start:end]) for start in range(len(tokens)) for end in range(start + 1, len(tokens) + 1)}


def prioritize_options(api_data, guessed):
    if not api_data:
        return []

    options = []
    if isinstance(api_data, list):
        for opt in api_data:
            if isinstance(opt, dict) and "code" in opt and "name" in opt:
                # Keep other fields the court sends (e.g. "amountincontroversy" on
                # filing types) available to callers that need more than value/text,
                # without every caller having to know the raw Tyler field names.
                extra = {key: value for key, value in opt.items() if key not in ("code", "name")}
                options.append({"value": opt["code"], "text": opt["name"], **extra})
    options.sort(key=lambda x: x["text"])

    if not guessed:
        return options

    guessed_norm = guessed.lower().strip() if guessed else ""

    # Create prioritized list
    prioritized_options = []
    other_options = []

    for opt in options:
        option_text = opt.get("text", "").lower().strip()

        # Direct value match (e.g., 'cook' matches 'cook') or text match (e.g., 'Cook County' matches 'cook')
        # Edit distance used to count as a match here, but at any threshold loose
        # enough to forgive a typo it also pairs unrelated options ("Motion" and
        # "Notice" are 3 edits apart), and the marker below claims the document
        # actually said so.
        is_match = option_text == guessed_norm or option_text in guessed_norm or guessed_norm in option_text

        if is_match:
            # Mark matches from document extraction with a compact marker.
            opt_copy = opt.copy()
            opt_copy["text"] = f"{opt['text']} *"
            prioritized_options.append(opt_copy)
        else:
            other_options.append(opt)

    # Mark only the first prioritized court as selected/default
    final_options = prioritized_options + other_options
    if prioritized_options:
        # Mark the first recommended court as selected using multiple flag approaches
        final_options[0]["selected"] = True
        final_options[0]["default"] = True
        final_options[0]["recommended"] = True

    return final_options


class DropdownAPIViews(APIResponseMixin):
    """API views for dropdown data"""

    @staticmethod
    @require_http_methods(["GET"])
    def get_case_categories(request):
        """Get available case categories from Suffolk LIT Lab API"""
        try:
            # Get required parameters
            court_code = request.GET.get("court")
            jurisdiction = get_jurisdiction_from_request(request)
            guessed_category = request.GET.get("guessed_case_category")

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisdiction parameter")

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            # Make API call to external categories endpoint
            api_url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{court_code}/categories"
            timing = "Subsequent" if request.GET.get("existing_case") == "yes" else "Initial"
            params = {"fileable_only": True, "timing": timing}

            # Make the API request with auth tokens if available
            headers = {}

            logger.debug("GET %s header keys=%s", api_url, list(headers.keys()))
            response = requests.get(api_url, params=params, headers=headers, timeout=30)
            logger.debug(
                "Categories response: status=%s body=%s",
                response.status_code,
                response.content,
            )

            if response.status_code == 200:
                # Parse the response (expecting list of {name, code}), and put the most relevant options at the top
                categories = prioritize_options(response.json(), guessed_category)
                return DropdownAPIViews.success_response(categories)
            else:
                return DropdownAPIViews.error_response(f"API request failed with status {response.status_code}")

        except (requests.RequestException, requests.Timeout) as api_error:
            logger.warning("Categories API request failed: %s", api_error)
            return DropdownAPIViews.error_response(f"API request failed: {str(api_error)}")
        except Exception as e:
            logger.exception("Unexpected error in get_case_categories")
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    def get_case_types(request):
        """Get case types based on selected category from Suffolk LIT Lab API"""
        try:
            # Get required parameters
            court_code = request.GET.get("court")
            category_id = request.GET.get("parent")  # category_id from case category dropdown
            jurisdiction = get_jurisdiction_from_request(request)
            guessed_type = request.GET.get("guessed_case_type")

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisdiction parameter")

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            if not category_id:
                return DropdownAPIViews.error_response("Missing required category_id parameter")

            # Make API call to external case types endpoint
            path = f"/jurisdictions/{jurisdiction}/codes/courts/{court_code}/case_types/"
            api_url = f"{settings.EFSP_URL}{path}"
            timing = "Subsequent" if request.GET.get("existing_case") == "yes" else "Initial"
            params = {"category_id": category_id, "timing": timing}

            # Make the API request with auth tokens if available
            headers = {}

            logger.debug("GET %s header keys=%s", api_url, list(headers.keys()))
            response = requests.get(api_url, params=params, headers=headers, timeout=30)
            logger.debug(
                "Case types response: status=%s body=%s",
                response.status_code,
                response.text,
            )

            if response.status_code == 200:
                # Preserve suggestion markers and selection while displaying filing codes alphabetically
                case_types = prioritize_options(response.json(), guessed_type)
                return DropdownAPIViews.success_response(case_types)

        except Exception:
            logger.exception("Unexpected error in get_case_types")

    @staticmethod
    @require_http_methods(["GET"])
    def get_filing_types(request):
        """Get filing types based on selected case type from Suffolk LIT Lab API"""
        try:
            # Get required parameters - support both parameter names for flexibility
            court_code = request.GET.get("court")
            case_type_id = request.GET.get("case_type") or request.GET.get("parent")  # Support both flows
            case_category_id = request.GET.get("case_category")
            jurisdiction = get_jurisdiction_from_request(request)
            existing_case = request.GET.get("existing_case")
            guessed_filing_type = request.GET.get("guessed_filing_type")

            # Set initial flag based on existing_case parameter:
            # - "yes" means existing case, so initial=False (not an initial filing)
            # - "no" means new case, so initial=True (is an initial filing)
            # - Default to True if not specified
            if existing_case == "yes":
                initial = "false"
            else:
                initial = "true"

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            if not case_type_id:
                return DropdownAPIViews.error_response("Missing required case_type parameter")

            # Make API call to external filing types endpoint - use case_type_id as category_id
            path = (
                f"/jurisdictions/{jurisdiction}/codes/courts/{court_code}/filing_types/"
                f"?initial={initial}&category_id={case_category_id}&type_id={case_type_id}"
            )
            api_url = f"{settings.EFSP_URL}{path}"

            # Make the API request with auth tokens if available
            headers = {}

            logger.debug("GET %s header keys=%s", api_url, list(headers.keys()))
            response = requests.get(api_url, headers=headers, timeout=30)
            logger.debug(
                "Filing types response: status=%s content_type=%s",
                response.status_code,
                response.headers.get("Content-Type"),
            )

            if response.status_code == 200:
                # Preserve suggestion markers and selection while displaying filing codes alphabetically
                filing_types = prioritize_options(response.json(), guessed_filing_type)
                filing_types.sort(key=lambda item: (item["text"].strip().casefold(), str(item["value"])))
                return DropdownAPIViews.success_response(filing_types)
            else:
                return DropdownAPIViews.error_response(f"API request failed with status {response.status_code}")

        except (requests.RequestException, requests.Timeout) as api_error:
            return DropdownAPIViews.error_response(f"API request failed: {str(api_error)}")
        except Exception as e:
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    def get_courts(request):
        """Get available courts based on user location/preferences"""
        try:
            jurisdiction = get_jurisdiction_from_request(request)
            user_zip = request.GET.get("user_zip")
            user_county = request.GET.get("user_county")
            guessed_court = request.GET.get("guessed_court", "")

            # Make API call to external jurisdiction endpoint
            api_url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/"
            # `fileable_only=true` is incomplete on the EFSP test service and
            # hides courts that do expose valid filing categories. Later
            # dropdown calls still validate the chosen court's hierarchy.
            params = {"fileable_only": False, "with_names": True}

            try:
                # Make the API request with auth tokens if available
                headers = {}

                logger.debug("GET %s header keys=%s", api_url, list(headers.keys()))
                response = requests.get(api_url, params=params, headers=headers, timeout=10)
                logger.debug(
                    "Courts response: status=%s content_type=%s",
                    response.status_code,
                    response.headers.get("Content-Type"),
                )

                if response.status_code == 200:
                    # Parse the API response - expecting list of {name, code} objects
                    api_data = response.json()

                    # Transform API data to our dropdown format
                    courts = []
                    if isinstance(api_data, list):
                        headings = heading_court_codes(
                            jurisdiction, [str(court.get("code")) for court in api_data if isinstance(court, dict)]
                        )
                        for court in api_data:
                            if isinstance(court, dict) and "code" in court and "name" in court:
                                # Rows that exist only inside Tyler -- test
                                # fixtures, retired locations -- are not courts
                                # anyone can file into.
                                if is_non_filing_court(court["name"]) or str(court["code"]) in headings:
                                    continue

                                courts.append({"value": court["code"], "text": court["name"]})

                    # If we got courts from the API, return them with user location priority
                    if courts:
                        return DropdownAPIViews.success_response(
                            DropdownAPIViews._prioritize_courts_by_location(
                                courts, guessed_court, user_zip, user_county
                            )
                        )
                    else:
                        # If no courts found in API response, fall back to hardcoded data
                        raise requests.RequestException("No courts found in API response")

                else:
                    # API call failed, fall back to hardcoded Illinois courts
                    raise requests.RequestException(f"API returned status {response.status_code}")

            except (requests.RequestException, requests.Timeout):
                # Fallback to comprehensive Illinois courts if API fails
                logger.warning("Courts API failed; using fallback list")
                fallback_courts = [
                    {"value": "PSUPCRT", "text": "Supreme Court of Illinois"},
                    {"value": "PAC1", "text": "Appellate Court – 1st District"},
                    {"value": "PAC2", "text": "Appellate Court – 2nd District"},
                    {"value": "PAC3", "text": "Appellate Court – 3rd District"},
                    {"value": "PAC4", "text": "Appellate Court – 4th District"},
                    {"value": "PAC5", "text": "Appellate Court – 5th District"},
                    {"value": "ardc", "text": "ARDC Clerk's Office"},
                    {"value": "adams", "text": "Adams County"},
                    {"value": "alexander", "text": "Alexander County"},
                    {"value": "bond", "text": "Bond County"},
                    {"value": "boone", "text": "Boone County"},
                    {"value": "brown", "text": "Brown County"},
                    {"value": "bureau", "text": "Bureau County"},
                    {"value": "calhoun", "text": "Calhoun County"},
                    {"value": "carroll", "text": "Carroll County"},
                    {"value": "cass", "text": "Cass County"},
                    {"value": "champaign", "text": "Champaign County"},
                    {"value": "christian", "text": "Christian County"},
                    {"value": "clark", "text": "Clark County"},
                    {"value": "clay", "text": "Clay County"},
                    {"value": "clinton", "text": "Clinton County"},
                    {"value": "coles", "text": "Coles County"},
                    {"value": "cook:chd1", "text": "Cook County - Chancery - District 1 - Chicago"},
                    {"value": "cook:cd1", "text": "Cook County - County Division - District 1 - Chicago"},
                    {"value": "cook:crd1", "text": "Cook County - Criminal - District 1 - Chicago"},
                    {"value": "cook:dr1", "text": "Cook County - Domestic Relations - District 1 - Chicago"},
                    {"value": "cook:law1", "text": "Cook County - Law - District 1 - Chicago"},
                    {"value": "cook:cvd1", "text": "Cook County - Municipal Civil - District 1 - Chicago"},
                    {"value": "cook:pr1", "text": "Cook County - Probate - District 1 - Chicago"},
                    {"value": "crawford", "text": "Crawford County"},
                    {"value": "cumberland", "text": "Cumberland County"},
                    {"value": "dewitt", "text": "De Witt County"},
                    {"value": "dekalb", "text": "DeKalb County"},
                    {"value": "douglas", "text": "Douglas County"},
                    {"value": "dupage", "text": "DuPage County"},
                    {"value": "edgar", "text": "Edgar County"},
                    {"value": "edwards", "text": "Edwards County"},
                    {"value": "effingham", "text": "Effingham County"},
                    {"value": "fayette", "text": "Fayette County"},
                    {"value": "ford", "text": "Ford County"},
                    {"value": "franklin", "text": "Franklin County"},
                    {"value": "fulton", "text": "Fulton County"},
                    {"value": "gallatin", "text": "Gallatin County"},
                    {"value": "greene", "text": "Greene County"},
                    {"value": "grundy", "text": "Grundy County"},
                    {"value": "hamilton", "text": "Hamilton County"},
                    {"value": "hancock", "text": "Hancock County"},
                    {"value": "hardin", "text": "Hardin County"},
                    {"value": "henderson", "text": "Henderson County"},
                    {"value": "henry", "text": "Henry County"},
                    {"value": "iroquois", "text": "Iroquois County"},
                    {"value": "jackson", "text": "Jackson County"},
                    {"value": "jasper", "text": "Jasper County"},
                    {"value": "jefferson", "text": "Jefferson County"},
                    {"value": "jersey", "text": "Jersey County"},
                    {"value": "jodaviess", "text": "Jo Daviess County"},
                    {"value": "johnson", "text": "Johnson County"},
                    {"value": "kane", "text": "Kane County"},
                    {"value": "KankakeeCV", "text": "Kankakee - Civil"},
                    {"value": "KankakeeCR", "text": "Kankakee - Criminal"},
                    {"value": "KankakeeFAM", "text": "Kankakee - Family and Juvenile"},
                    {"value": "KankakeeTR", "text": "Kankakee - Traffic"},
                    {"value": "kendall", "text": "Kendall County"},
                    {"value": "knox", "text": "Knox County"},
                    {"value": "lasalle", "text": "LaSalle County"},
                    {"value": "lake", "text": "Lake County"},
                    {"value": "lawrence", "text": "Lawrence County"},
                    {"value": "lee", "text": "Lee County"},
                    {"value": "livingston", "text": "Livingston County"},
                    {"value": "logan", "text": "Logan County"},
                    {"value": "macon", "text": "Macon County"},
                    {"value": "macoupin", "text": "Macoupin County"},
                    {"value": "madison", "text": "Madison County"},
                    {"value": "marion", "text": "Marion County"},
                    {"value": "marshall", "text": "Marshall County"},
                    {"value": "mason", "text": "Mason County"},
                    {"value": "massac", "text": "Massac County"},
                    {"value": "mcdonough", "text": "McDonough County"},
                    {"value": "mchenry", "text": "McHenry County"},
                    {"value": "mclean", "text": "McLean County"},
                    {"value": "menard", "text": "Menard County"},
                    {"value": "mercer", "text": "Mercer County"},
                    {"value": "monroe", "text": "Monroe County"},
                    {"value": "montgomery", "text": "Montgomery County"},
                    {"value": "morgan", "text": "Morgan County"},
                    {"value": "moultrie", "text": "Moultrie County"},
                    {"value": "ogle", "text": "Ogle County"},
                    {"value": "peoria", "text": "Peoria County"},
                    {"value": "peoriacr", "text": "Peoria CR"},
                    {"value": "peoriacs", "text": "Peoria CS"},
                    {"value": "peoriatr", "text": "Peoria TR"},
                    {"value": "perry", "text": "Perry County"},
                    {"value": "piatt", "text": "Piatt County"},
                    {"value": "pike", "text": "Pike County"},
                    {"value": "pope", "text": "Pope County"},
                    {"value": "pulaski", "text": "Pulaski County"},
                    {"value": "putnam", "text": "Putnam County"},
                    {"value": "randolph", "text": "Randolph County"},
                    {"value": "richland", "text": "Richland County"},
                    {"value": "rockisland", "text": "Rock Island County"},
                    {"value": "saline", "text": "Saline County"},
                    {"value": "sangamon", "text": "Sangamon County"},
                    {"value": "schuyler", "text": "Schuyler County"},
                    {"value": "scott", "text": "Scott County"},
                    {"value": "shelby", "text": "Shelby County"},
                    {"value": "stclair", "text": "St. Clair County"},
                    {"value": "stark", "text": "Stark County"},
                    {"value": "stephenson", "text": "Stephenson County"},
                    {"value": "tazewell", "text": "Tazewell County"},
                    {"value": "tazewell:tr", "text": "Tazewell County - Traffic"},
                    {"value": "union", "text": "Union County"},
                    {"value": "vermilion", "text": "Vermilion County"},
                    {"value": "wabash", "text": "Wabash County"},
                    {"value": "warren", "text": "Warren County"},
                    {"value": "washington", "text": "Washington County"},
                    {"value": "wayne", "text": "Wayne County"},
                    {"value": "white", "text": "White County"},
                    {"value": "whiteside", "text": "Whiteside County"},
                    {"value": "will", "text": "Will County"},
                    {"value": "williamson", "text": "Williamson County"},
                    {"value": "woodford", "text": "Woodford County"},
                ]
                logger.debug("Returning fallback courts")
                return DropdownAPIViews.success_response(
                    DropdownAPIViews._prioritize_courts_by_location(
                        fallback_courts, guessed_court, user_zip, user_county
                    )
                )

        except Exception as e:
            logger.error("Error prioritizing courts by location: %s", str(e))
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    def _prioritize_courts_by_location(courts, guessed_court="", user_zip=None, user_county=None):
        """
        Prioritize courts based on user's location (zip code or county).
        Adds a 'default' flag to courts that match the user's location.
        """
        if not courts:
            return courts

        # Location data controls location recommendations. Keep the extracted
        # court guess separate so it can receive the extraction marker below.
        target_county = user_county
        if user_zip and not target_county:
            target_county = get_county_by_zip(user_zip)

        if not target_county and not guessed_court:
            return courts

        guessed_keys = _county_keys(guessed_court)
        guessed_key = "".join(_county_tokens(guessed_court))
        target_key = "".join(_county_tokens(target_county))

        # Create prioritized list
        guessed_courts = []
        exact_guessed_courts = []
        location_courts = []
        other_courts = []

        for court in courts:
            court_value = court.get("value", "").lower()
            # Cook County's divisions all share a "cook:" prefix, so the county
            # is whatever sits in front of the colon.
            court_county = court_value.split(":", 1)[0].replace(" ", "")
            court_key = "".join(_county_tokens(court.get("text", "")))
            court_keys = _county_keys(court.get("text", "")) | {court_county}

            guessed_match = bool(guessed_keys) and court_county in guessed_keys
            location_match = bool(target_key) and target_key in court_keys

            if guessed_match:
                # A document match gets the extraction marker, and comes first:
                # the document is better evidence of the court than a zip code.
                court_copy = court.copy()
                court_copy["text"] = f"{court['text']} *"
                guessed_courts.append(court_copy)
                if court_key == guessed_key:
                    exact_guessed_courts.append(court_copy)
            elif location_match:
                # Location-only recommendations keep their existing wording.
                court_copy = court.copy()
                court_copy["text"] = f"{court['text']} (Recommended)"
                location_courts.append(court_copy)
            else:
                other_courts.append(court)

        # Only pre-select a court the evidence actually singles out. A caption
        # reading "Circuit Court of Cook County" matches every Cook division,
        # and picking one of them for the filer would be a guess the document
        # never made. Leave those at the top of the list and let them choose.
        selected_court = None
        if len(guessed_courts) == 1:
            selected_court = guessed_courts[0]
        elif len(exact_guessed_courts) == 1:
            selected_court = exact_guessed_courts[0]
        elif not guessed_courts and location_courts:
            selected_court = location_courts[0]

        if selected_court is not None:
            # Mark the recommended court as selected using multiple flag approaches
            selected_court["selected"] = True
            selected_court["default"] = True
            selected_court["recommended"] = True

        return guessed_courts + location_courts + other_courts

    @staticmethod
    @require_http_methods(["GET"])
    def get_document_types(request):
        """Get document types based on selected filing type from Suffolk LIT Lab API"""
        try:
            # Get required parameters
            court_code = request.GET.get("court")
            filing_type_id = request.GET.get("parent")  # filing type ID from filing type dropdown
            jurisdiction = request.GET.get("jurisdiction")

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisdiction parameter")

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            if not filing_type_id:
                return DropdownAPIViews.error_response("Missing required filing type parameter")

            # Make API call to external document types endpoint
            path = (
                f"/jurisdictions/{jurisdiction}/codes/courts/{court_code}/filing_types/{filing_type_id}/document_types"
            )
            api_url = f"{settings.EFSP_URL}{path}"
            logger.debug("GET %s", api_url)

            # Make the API request with auth tokens if available
            headers = {}

            response = requests.get(api_url, headers=headers, timeout=30)

            if response.status_code == 200:
                # Parse the API response - expecting list of {name, code} objects
                api_data = response.json()

                # Transform API data to our dropdown format
                document_types = []
                if isinstance(api_data, list):
                    for document_type in api_data:
                        if isinstance(document_type, dict) and "code" in document_type and "name" in document_type:
                            lower_name = document_type["name"].lower().strip()
                            is_non_confidential = lower_name in {"non-confidential", "public"}
                            if is_non_confidential:
                                text = f"No ({document_type['name']})"
                            else:
                                text = f"Yes ({document_type['name']})"

                            document_types.append(
                                {
                                    "value": document_type["code"],
                                    "text": text,
                                    "confidentiality": "non_confidential" if is_non_confidential else "confidential",
                                }
                            )

                return DropdownAPIViews.success_response(document_types)
            else:
                return DropdownAPIViews.error_response(f"API request failed with status {response.status_code}")

        except (requests.RequestException, requests.Timeout) as api_error:
            return DropdownAPIViews.error_response(f"API request failed: {str(api_error)}")
        except Exception as e:
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    def get_name_suffixes(request):
        """Get the court's accepted name suffixes (Jr., Sr., II, ...).

        A suffix has to exactly match one of these for Tyler to accept the
        party -- it isn't free text, even though it looks like it could be.
        """
        try:
            court_code = request.GET.get("court")
            jurisdiction = get_jurisdiction_from_request(request)

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisdiction parameter")

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            api_url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{court_code}/name_suffixes"
            logger.debug("GET %s", api_url)
            response = requests.get(api_url, timeout=10)

            if response.status_code == 200:
                api_data = response.json()
                suffixes = (
                    [
                        {"value": item["code"], "text": item["name"]}
                        for item in api_data
                        if isinstance(item, dict) and item.get("code") and item.get("name")
                    ]
                    if isinstance(api_data, list)
                    else []
                )
                return DropdownAPIViews.success_response(suffixes)
            else:
                return DropdownAPIViews.error_response(f"API request failed with status {response.status_code}")

        except (requests.RequestException, requests.Timeout) as api_error:
            return DropdownAPIViews.error_response(f"API request failed: {str(api_error)}")
        except Exception as e:
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    def get_optional_services(request):
        """Get optional services for a filing type from Suffolk LIT Lab API"""
        try:
            # Get required parameters
            court_code = request.GET.get("court")
            filing_type_id = request.GET.get("filing_type_id")
            jurisdiction = request.GET.get("jurisdiction")

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisdiction parameter")

            if not court_code:
                return DropdownAPIViews.error_response("Missing required court parameter")

            if not filing_type_id:
                return DropdownAPIViews.error_response("Missing required filing_type_id parameter")

            # Make API call to Suffolk optional services endpoint
            path = (
                f"/jurisdictions/{jurisdiction}/codes/courts/{court_code}/filing_types/"
                f"{filing_type_id}/optional_services"
            )
            api_url = f"{settings.EFSP_URL}{path}"

            try:
                # Make the API request with auth tokens if available
                headers = {}

                response = requests.get(api_url, headers=headers, timeout=30)

                if response.status_code == 200:
                    # Read through the payload normalizer's own parser, so the
                    # picker and the submitted payload cannot disagree about
                    # which services take a multiplier.
                    return DropdownAPIViews.success_response(parse_optional_services(response.json()))

                else:
                    # API call failed, return empty list with info message
                    msg = (
                        "Optional services API returned status "
                        f"{response.status_code} for court {court_code}, "
                        f"filing type {filing_type_id}"
                    )
                    logger.info(msg)
                    return DropdownAPIViews.success_response([])

            except (requests.RequestException, requests.Timeout) as api_error:
                # Return empty list if API fails
                msg = (
                    "Optional services API request failed for court "
                    f"{court_code}, filing type {filing_type_id}: {api_error}"
                )
                logger.warning(msg)
                return DropdownAPIViews.success_response([])

        except Exception as e:
            return DropdownAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    def get_party_types(request):
        """Get available party types from Suffolk LIT Lab API based on case type"""
        try:
            # Get required parameters
            court_code = request.GET.get("court")
            case_type_code = request.GET.get("case_type")
            jurisdiction = request.GET.get("jurisdiction")
            only_required = request.GET.get("only_required", "True").lower() == "true"

            if not jurisdiction:
                return DropdownAPIViews.error_response("Missing required jurisidiction parameter")

            if not court_code or not case_type_code:
                return DropdownAPIViews.error_response("Missing required court or case_type parameters")

            # Make API call to external party types endpoint
            path = f"/jurisdictions/{jurisdiction}/codes/courts/{court_code}/case_types/{case_type_code}/party_types"
            api_url = f"{settings.EFSP_URL}{path}"

            # Make the API request with auth tokens if available
            headers = {}

            response = requests.get(api_url, headers=headers, timeout=10)

            if response.status_code == 200:
                # Parse the API response - expecting list of party type objects
                api_data = response.json()

                # Transform API data to our dropdown format
                party_types = []
                if isinstance(api_data, list):
                    for party_type in api_data:
                        if isinstance(party_type, dict) and "code" in party_type and "name" in party_type:
                            # Include all party types for now (no filtering)
                            is_required = party_type.get("isrequired", False)
                            if not is_required and only_required:
                                continue

                            party_types.append(
                                {
                                    "value": party_type["code"],
                                    "text": party_type["name"],
                                    "code": party_type["code"],
                                    "name": party_type["name"],
                                    "isRequired": is_required,
                                    "isAvailableForNewParties": party_type.get("isAvailableForNewParties", True),
                                }
                            )

                    # Sort alphabetically by name
                    party_types.sort(key=lambda x: x["name"])

                return DropdownAPIViews.success_response(party_types)
            else:
                # Log the error but don't expose sensitive details
                error_msg = f"External API returned status {response.status_code}"
                return DropdownAPIViews.error_response(error_msg)

        except requests.RequestException as e:
            logger.warning("Party types API request failed: %s", e)
            return DropdownAPIViews.error_response("Network error: Failed to fetch party types")
        except Exception as e:
            logger.exception("Unexpected error in get_party_types")
            return DropdownAPIViews.error_response(f"Unexpected error: {str(e)}")


# Individual view functions for URL mapping
get_case_categories = DropdownAPIViews.get_case_categories
get_case_types = DropdownAPIViews.get_case_types
get_filing_types = DropdownAPIViews.get_filing_types
get_courts = DropdownAPIViews.get_courts
get_document_types = DropdownAPIViews.get_document_types
get_optional_services = DropdownAPIViews.get_optional_services
get_party_types = DropdownAPIViews.get_party_types
get_name_suffixes = DropdownAPIViews.get_name_suffixes
