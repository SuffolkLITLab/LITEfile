"""
API views for authentication and user management
"""

import json
import logging

import requests
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from requests.exceptions import RequestException, Timeout

from efile.api.suffolk_api_views import get_tyler_token
from efile.services.account_profile import fetch_account_profile
from efile.services.court_payments import BANK_ACCOUNT_TYPE, bank_payment_policy, policy_message
from efile.services.current_drafts import get_current_draft
from efile.services.payment_accounts import is_active
from efile.utils.efsp_security import require_efsp_login, validated_jurisdiction
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request

from .base import APIResponseMixin

logger = logging.getLogger(__name__)


class AuthAPIViews(APIResponseMixin):
    """API views for authentication"""

    @staticmethod
    @require_http_methods(["POST"])
    def user_login(request):
        """Handle user login"""
        if request.content_type != "application/json":
            return AuthAPIViews.error_response("Expected application/json", 415)
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                return AuthAPIViews.error_response("Expected a JSON object", 400)

            username = data.get("username")
            password = data.get("password")
            jurisdiction = validated_jurisdiction(data.get("jurisdiction"))

            if not username or not password or not jurisdiction:
                return AuthAPIViews.error_response("Username, password, and jurisdiction required")

            user = authenticate(request, username=username, password=password, jurisdiction=jurisdiction)

            if user is not None:
                auth_tokens = request.session.get("auth_tokens", {})
                request.session.flush()
                login(request, user)
                request.session["auth_tokens"] = auth_tokens
                request.session["user_email"] = user.email
                request.session["jurisdiction"] = jurisdiction
                return AuthAPIViews.success_response(
                    {
                        "user_id": user.id,
                        "username": user.account_email,
                        "email": user.email,
                        "is_authenticated": True,
                    },
                    "Login successful",
                )
            else:
                return AuthAPIViews.error_response("Invalid credentials", 401)

        except (ValueError, UnicodeDecodeError):
            return AuthAPIViews.error_response("Invalid login data", 400)
        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["POST"])
    def user_logout(request):
        """Handle user logout"""
        from efile.authentication import SuffolkEFileBackend

        try:
            SuffolkEFileBackend.logout(request)
            return AuthAPIViews.success_response({}, "Logout successful")
        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    @require_efsp_login
    def user_profile(request):
        """Get current user profile from external Suffolk eFile API"""
        try:
            # TODO(brycew): get some of this from the existing logged in user
            jurisdiction = get_jurisdiction_from_request(request)
            profile = fetch_account_profile(request, jurisdiction)
            if profile is None:
                return AuthAPIViews.error_response("Unable to retrieve profile", 500)

            # Local user data (if authenticated) combined with the account data.
            user_data = {
                "external_firm_data": profile["firm"],
                "id": request.user.id if request.user.is_authenticated else None,
                "username": request.user.account_email if request.user.is_authenticated else "guest",
                "email": request.user.email if request.user.is_authenticated else request.session.get("user_email"),
                "first_name": profile["first_name"],
                "last_name": profile["last_name"],
                "date_joined": request.user.date_joined.isoformat() if request.user.is_authenticated else None,
                "last_login": request.user.last_login.isoformat()
                if (request.user.is_authenticated and request.user.last_login)
                else None,
                # Address information from external API
                "address": profile["address"],
                "address_line2": profile["address_line2"],
                "city": profile["city"],
                "state": profile["state"],
                "zip": profile["zip"],
                "phone": profile["phone"],
                "zip_code": profile["zip"],
            }

            return AuthAPIViews.success_response(user_data)
        except Timeout:
            return AuthAPIViews.error_response("External API request timed out", 408)
        except Exception:
            logger.exception("Request exception")
            return AuthAPIViews.error_response("Request Exception", 500)

    @staticmethod
    @require_http_methods(["GET"])
    def external_profile(request):
        """Get additional profile data from external sources"""
        try:
            if not request.user.is_authenticated:
                return AuthAPIViews.error_response("Not authenticated", 401)

            # This could fetch additional data from Suffolk eFile API or other sources
            # For now, return enhanced location data
            external_data = {
                "location_details": {
                    "county_court_info": {
                        "cook": {"address": "50 W Washington St, Chicago, IL", "phone": "(312) 603-5030"},
                        "dupage": {"address": "505 N County Farm Rd, Wheaton, IL", "phone": "(630) 407-8700"},
                        "kane": {"address": "37W777 Route 38, St Charles, IL", "phone": "(630) 232-3413"},
                    }
                },
                "preferences": {
                    "language": "en",
                    "timezone": "America/Chicago",
                    "notification_settings": {"email": True, "sms": False},
                },
            }

            return AuthAPIViews.success_response(external_data)

        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    @require_efsp_login
    def tyler_token(request):
        """Return only the signed-in user's token for the payment form."""
        try:
            # Get jurisdiction and Tyler token dynamically
            jurisdiction = get_jurisdiction_from_request(request)
            tyler_token = get_tyler_token(request, jurisdiction)
            return AuthAPIViews.success_response({"tyler_token": tyler_token, "state": jurisdiction})
        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    @require_efsp_login
    def payment_accounts(request):
        """Get payment accounts from Suffolk eFile API with proper authentication"""
        try:
            # Get jurisdiction and Tyler token dynamically
            jurisdiction = get_jurisdiction_from_request(request)
            tyler_token = get_tyler_token(request, jurisdiction)
            api_key = getattr(settings, "SUFFOLK_EFILE_API_KEY", None)

            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"{jurisdiction.title()}-eFile-Client/1.0",
                "X-API-Key": api_key if api_key else "",
            }

            # Add Tyler token if available
            if tyler_token:
                headers[f"tyler-token-{jurisdiction}"] = tyler_token
            else:
                # Log that no token was found for debugging
                logger.info(
                    "No Tyler token found for state '%s' in Suffolk eFile payment accounts request",
                    jurisdiction,
                )

            url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/payments/payment-accounts/"
            logger.debug("GET %s header keys=%s", url, list(headers.keys()))
            api_response = requests.get(url, headers=headers, timeout=10, allow_redirects=False)
            logger.debug(
                "Payment accounts response: status=%s content_type=%s",
                api_response.status_code,
                api_response.headers.get("Content-Type"),
            )

            if api_response.status_code == 200:
                payment_accounts = api_response.json()
                if not isinstance(payment_accounts, list):
                    return AuthAPIViews.error_response("We could not load payment methods. Please try again.", 503)
                payment_accounts = [
                    account
                    for account in payment_accounts
                    if isinstance(account, dict) and account.get("paymentAccountID") and is_active(account)
                ]
                draft = get_current_draft(request, jurisdiction=jurisdiction, resume_latest=False)
                if draft is not None and any(
                    a.get("paymentAccountTypeCode") == BANK_ACCOUNT_TYPE for a in payment_accounts
                ):
                    policy = bank_payment_policy(jurisdiction, draft.court_code)
                    if policy != "allowed":
                        payment_accounts = [
                            account
                            for account in payment_accounts
                            if account.get("paymentAccountTypeCode") != BANK_ACCOUNT_TYPE
                        ]
                    return JsonResponse(
                        {
                            "success": True,
                            "data": payment_accounts,
                            "bank_payment_policy": policy,
                            "bank_payment_message": policy_message(policy),
                        }
                    )
                return AuthAPIViews.success_response(payment_accounts)
            elif api_response.status_code == 401:
                # Return empty list for unauthorized - frontend will show "add new payment method"
                return AuthAPIViews.success_response([])
            else:
                # Return error with status info
                return AuthAPIViews.error_response(
                    f"Payment accounts API returned status {api_response.status_code}: {api_response.text[:200]}",
                    api_response.status_code,
                )

        except Timeout:
            return AuthAPIViews.error_response("Payment accounts API request timed out", 408)
        except RequestException as e:
            return AuthAPIViews.error_response(f"Could not connect to payment accounts API: {str(e)}", 503)
        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")

    @staticmethod
    @require_http_methods(["GET"])
    @require_efsp_login
    def payment_account_types(request):
        """List the court's payment account types (e.g. "CC", "WV") with names.

        A payment account only carries its type *code*, not a human-readable
        name, so the frontend needs this list to label account types other
        than the ones it special-cases (card, waiver).
        """
        try:
            jurisdiction = get_jurisdiction_from_request(request)
            tyler_token = get_tyler_token(request, jurisdiction)
            api_key = getattr(settings, "SUFFOLK_EFILE_API_KEY", None)

            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"{jurisdiction.title()}-eFile-Client/1.0",
                "X-API-Key": api_key if api_key else "",
            }
            if tyler_token:
                headers[f"tyler-token-{jurisdiction}"] = tyler_token

            url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/payments/types"
            logger.debug("GET %s header keys=%s", url, list(headers.keys()))
            api_response = requests.get(url, headers=headers, timeout=10, allow_redirects=False)

            if api_response.status_code == 200:
                return AuthAPIViews.success_response(api_response.json())
            elif api_response.status_code == 401:
                return AuthAPIViews.success_response([])
            else:
                return AuthAPIViews.error_response(
                    f"Payment account types API returned status {api_response.status_code}: {api_response.text[:200]}",
                    api_response.status_code,
                )

        except Timeout:
            return AuthAPIViews.error_response("Payment account types API request timed out", 408)
        except RequestException as e:
            return AuthAPIViews.error_response(f"Could not connect to payment account types API: {str(e)}", 503)
        except Exception as e:
            return AuthAPIViews.error_response(f"Error: {str(e)}")


# Individual view functions for URL mapping
user_login = AuthAPIViews.user_login
user_logout = AuthAPIViews.user_logout
user_profile = AuthAPIViews.user_profile
external_profile = AuthAPIViews.external_profile
payment_accounts = AuthAPIViews.payment_accounts
payment_account_types = AuthAPIViews.payment_account_types
tyler_token = AuthAPIViews.tyler_token
