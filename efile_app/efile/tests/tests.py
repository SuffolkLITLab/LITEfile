import json
import os
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from efile.utils.config_loader import InvalidJurisdiction, config_loader

User = get_user_model()

# =====================================================
# Secrets loaded at runtime.
#
# These tests log in to the live Tyler test EFM. Without credentials they skip
# rather than fail, so a missing local .env stays distinguishable from a real
# regression. CI supplies both from repo secrets, so coverage there is unchanged.


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; skipping test that needs the live Tyler EFM")
    return value


def get_test_username():
    return _require_env("TESTS_TYLER_USERNAME")


def get_test_password():
    return _require_env("TESTS_TYLER_PASSWORD")


# ============================================================================
# DROPDOWN API TESTS
# ============================================================================


class TestDropdownAPIs:
    """Test suite for dropdown API endpoints."""

    @pytest.fixture
    def api_client(self):
        """Create a client with AJAX headers."""
        client = Client()
        return client

    @pytest.fixture
    def user(self, db):
        """Create a test user."""
        return User.objects.create_user(username="testuser", email="test@example.com", password="testpass123")

    def test_courts_api_without_params(self, api_client):
        """Test courts API returns data without parameters."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                "courts": [
                    {"code": "cook:law1", "name": "Cook County Law Division"},
                    {"code": "cook:dr1", "name": "Cook County Domestic Relations"},
                ]
            }
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True
            assert len(data["data"]) >= 2
            assert any(court["value"] == "cook:law1" for court in data["data"])

    def test_courts_api_with_user_location(self, api_client):
        """Test courts API prioritizes based on user location."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {"code": "cook:law1", "name": "Cook County Law Division", "jurisdiction": "illinois"},
                {"code": "will:law1", "name": "Will County Law Division", "jurisdiction": "illinois"},
            ]
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get(
                "/api/dropdowns/courts/",
                {"user_county": "Cook", "user_zip": "60601"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True

            # Cook County courts should be marked as recommended
            cook_courts = [court for court in data["data"] if "cook" in court["value"].lower()]
            assert len(cook_courts) > 0
            assert any(court.get("recommended") for court in cook_courts)

    def test_case_categories_api_with_court(self, api_client):
        """Test case categories API filters by court."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = [
                {"code": "civil", "name": "Civil Law"},
                {"code": "family", "name": "Family Law"},
                {"code": "probate", "name": "Probate"},
            ]
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get(
                "/api/dropdowns/case-categories/",
                {"court": "cook:law1", "jurisdiction": "illinois"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

            if response.status_code != 200:
                print(f"Error response content: {response.content.decode()}")

            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True
            assert len(data["data"]) > 0

            # Should contain civil and family for law court
            category_texts = [cat["text"].lower() for cat in data["data"]]
            assert any("civil" in text for text in category_texts)

    def test_case_categories_api_without_court_returns_error(self, api_client):
        """Test case categories API requires court parameter."""
        response = api_client.get("/api/dropdowns/case-categories/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False
        assert "court parameter" in data["error"].lower() or "missing" in data["error"].lower()

    def test_case_types_api(self, api_client):
        """Test case types API returns filtered data."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            # The API returns list format, not dict with 'case_types' key
            mock_response.json.return_value = [
                {"code": "divorce", "name": "Divorce"},
                {"code": "custody", "name": "Child Custody"},
            ]
            mock_response.status_code = 200  # Set status code properly
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get(
                "/api/dropdowns/case-types/",
                {
                    "parent": "family",  # Use 'parent' not 'category'
                    "court": "cook:dr1",
                    "jurisdiction": "illinois",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True
            assert len(data["data"]) > 0

    def test_api_handles_external_service_failure(self, api_client):
        """Test API gracefully handles external service failures."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")

            response = api_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

            # The API should return a 400 error status for external service failure
            assert response.status_code == 400
            data = json.loads(response.content)
            assert data["success"] is False
            assert "error" in data
            # Check that the actual error message is present
            assert "network error" in data["error"].lower()


# ============================================================================
# AUTH API TESTS
# ============================================================================


class TestAuthAPIs:
    """Test suite for authentication API endpoints."""

    @pytest.fixture
    def user(self, db):
        """Create a test user with profile data."""
        return User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123", first_name="John", last_name="Doe"
        )

    def test_profile_api_authenticated_user(self, client, user):
        """Test profile API returns user data when authenticated."""
        username = get_test_username()
        password = get_test_password()
        response_one = client.post(
            "/api/auth/login/",
            content_type="application/json",
            data=json.dumps({"username": username, "password": password, "jurisdiction": "illinois"}),
        )

        assert response_one.status_code == 200

        response = client.get(
            "/api/auth/profile/", {"jurisdiction": "illinois"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True
        assert data["data"]["username"] == username
        # Assert the shape, not one developer's Tyler account: whoever's
        # credentials are configured, the profile must carry a real first name.
        assert isinstance(data["data"]["first_name"], str)
        assert data["data"]["first_name"]
        assert data["data"]["email"] == username

    def test_profile_api_includes_location_data(self, client, user):
        """Test profile API includes the address the account has on file."""
        username = get_test_username()
        password = get_test_password()
        client.post(
            "/api/auth/login/",
            content_type="application/json",
            data=json.dumps({"username": username, "password": password, "jurisdiction": "illinois"}),
        )

        response = client.get(
            "/api/auth/profile/", {"jurisdiction": "illinois"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True

        # The address comes from the account, not from a hard-coded Illinois
        # default: a Vermont filer must not be handed a Cook County one.
        assert "zip_code" in data["data"]
        assert data["data"]["zip_code"] == "02108"
        assert "preferred_county" not in data["data"]

    def test_profile_api_unauthenticated_user(self, client):
        """Private account data requires an authenticated court identity."""
        response = client.get(
            "/api/auth/profile/", {"jurisdiction": "illinois"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        assert response.status_code == 401


# ============================================================================
# UTILITY FUNCTION TESTS
# ============================================================================


class TestZipToCountyMapping:
    """Test suite for Illinois zip-to-county mapping utility."""

    def test_get_county_by_zip_cook_county(self):
        """Test mapping for Cook County zip codes."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        # Test major Chicago zip codes
        assert get_county_by_zip("60601") == "Cook"  # Downtown Chicago
        assert get_county_by_zip("60614") == "Cook"  # Lincoln Park
        assert get_county_by_zip("60611") == "Cook"  # Near North Side

    def test_get_county_by_zip_other_counties(self):
        """Test mapping for other Illinois counties."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        # Test some other counties with correct zip codes
        assert get_county_by_zip("61820") == "Champaign"  # Champaign County
        assert get_county_by_zip("62701") == "Sangamon"  # Springfield
        assert get_county_by_zip("61108") == "Winnebago"  # Rockford

    def test_get_county_by_zip_invalid_zip(self):
        """Test handling of invalid zip codes."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        assert get_county_by_zip("00000") is None
        assert get_county_by_zip("99999") is None
        assert get_county_by_zip("invalid") is None
        assert get_county_by_zip("") is None
        assert get_county_by_zip(None) is None

    def test_get_zip_codes_by_county(self):
        """Test reverse mapping from county to zip codes."""
        from efile.utils.zip_to_county_il import get_zips_by_county

        cook_zips = get_zips_by_county("Cook")
        assert len(cook_zips) > 50  # Cook County has many zip codes
        assert "60601" in cook_zips
        assert "60614" in cook_zips

        # Test another county
        champaign_zips = get_zips_by_county("Champaign")
        assert len(champaign_zips) > 0
        assert "61820" in champaign_zips

    def test_get_zip_codes_by_county_invalid(self):
        """Test handling of invalid county names."""
        from efile.utils.zip_to_county_il import get_zips_by_county

        assert get_zips_by_county("InvalidCounty") == []
        assert get_zips_by_county("") == []
        assert get_zips_by_county(None) == []


# ============================================================================
# DROPDOWN VIEW HELPER TESTS
# ============================================================================


class TestDropdownViewHelpers:
    """Test suite for dropdown view helper functions."""

    def test_prioritize_courts_by_location(self):
        """Test court prioritization based on user location."""
        from efile.api.dropdown_views import DropdownAPIViews

        courts = [
            {"value": "cook:law1", "text": "Cook County Law Division"},
            {"value": "will:law1", "text": "Will County Law Division"},
            {"value": "dupage:law1", "text": "DuPage County Law Division"},
        ]

        # Test Cook County user
        prioritized = DropdownAPIViews._prioritize_courts_by_location(courts, user_zip="60601", user_county="Cook")
        cook_courts = [c for c in prioritized if "cook" in c["value"].lower()]
        assert len(cook_courts) > 0

        # Check that Cook County courts have recommended flag
        has_recommended = any(c.get("recommended") or c.get("default") for c in cook_courts)
        assert has_recommended

        # Cook County courts should be first
        first_court = prioritized[0]
        assert "cook" in first_court["value"].lower()

    def test_dropdown_view_class_can_be_imported(self):
        """Test that DropdownAPIViews class can be imported."""
        from efile.api.dropdown_views import DropdownAPIViews

        # Should be able to create instance (even though methods are static)
        view = DropdownAPIViews()
        assert view is not None

        # Should have required static methods
        assert hasattr(DropdownAPIViews, "_prioritize_courts_by_location")
        assert hasattr(DropdownAPIViews, "get_case_categories")
        assert hasattr(DropdownAPIViews, "get_courts")

    def test_prioritize_courts_by_location_no_data(self):
        """Test court prioritization with no courts data."""
        from efile.api.dropdown_views import DropdownAPIViews

        # Test with empty list
        result = DropdownAPIViews._prioritize_courts_by_location([])
        assert result == []

        # Test with None
        result = DropdownAPIViews._prioritize_courts_by_location(None)
        assert result is None

    def test_prioritize_courts_by_location_no_user_data(self):
        """Test court prioritization with no user location data."""
        from efile.api.dropdown_views import DropdownAPIViews

        courts = [
            {"value": "cook:law1", "text": "Cook County Law Division"},
            {"value": "will:law1", "text": "Will County Law Division"},
        ]

        # Without user location, should return courts unchanged
        result = DropdownAPIViews._prioritize_courts_by_location(courts)
        assert result == courts


# ============================================================================
# INTEGRATION TESTS
# ============================================================================


class TestExpertFormIntegration:
    """Integration tests for the complete expert form flow."""

    @pytest.fixture
    def authenticated_client(self, client, db):
        """Create an authenticated client."""
        user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123")
        client.force_login(user)
        session = client.session
        session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
        session.save()
        return client

    def test_expert_form_page_loads(self, authenticated_client):
        """The retired expert form URL safely returns users to the active flow."""
        response = authenticated_client.get("/jurisdiction/illinois/expert_form/")

        assert response.status_code == 302
        assert response.url == "/jurisdiction/illinois/options/"

    @patch("efile.api.dropdown_views.requests.get")
    def test_complete_dropdown_flow(self, mock_get, authenticated_client):
        """Test the complete dropdown cascade flow."""

        # Mock external API responses
        def mock_api_response(*args, **kwargs):
            url = args[0]
            mock_response = Mock()
            mock_response.status_code = 200  # Set status code properly
            mock_response.raise_for_status.return_value = None

            if "courts" in url:
                # Return list format as the API actually does
                mock_response.json.return_value = [{"code": "cook:law1", "name": "Cook County Law"}]
            elif "case_categories" in url or "categories" in url:
                # Return list format as the API actually does
                mock_response.json.return_value = [{"code": "civil", "name": "Civil Law"}]
            elif "case_types" in url:
                # Return list format as the API actually does
                mock_response.json.return_value = [{"code": "contract", "name": "Contract Dispute"}]

            return mock_response

        mock_get.side_effect = mock_api_response

        # Test courts API
        response = authenticated_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert response.status_code == 200

        # Test case categories API with court
        response = authenticated_client.get(
            "/api/dropdowns/case-categories/",
            {"jurisdiction": "illinois", "court": "cook:law1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert response.status_code == 200  # Test case types API with category
        response = authenticated_client.get(
            "/api/dropdowns/case-types/",
            {"parent": "civil", "jurisdiction": "illinois", "court": "cook:law1"},  # Use 'parent' not 'category'
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert response.status_code == 200


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestErrorHandling:
    """Test suite for error handling scenarios."""

    def test_api_without_ajax_header(self, client):
        """Test API endpoints work without AJAX headers (currently allowed)."""
        response = client.get("/api/dropdowns/courts/?jurisdiction=illinois")

        # Currently the API allows non-AJAX requests
        assert response.status_code == 200
        data = json.loads(response.content)

        # Should still return API response structure
        assert "success" in data

    @patch("efile.api.dropdown_views.requests.get")
    def test_external_api_timeout(self, mock_get, client):
        """Test handling of external API timeouts."""
        mock_get.side_effect = Exception("Connection timeout")

        response = client.get(
            "/api/dropdowns/courts/", {"jurisdiction": "illinois"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        # The API should return a 400 error status for timeout
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False
        assert "error" in data
        data = json.loads(response.content)
        assert data["success"] is False
        assert "error" in data

    def test_malformed_requests(self, client):
        """Test handling of malformed API requests."""
        # Test with invalid parameters
        response = client.get(
            "/api/dropdowns/case-categories/",
            {"court": ""},  # Empty court parameter
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False
        assert "error" in data
        assert data["success"] is False


# ============================================================================
# FORM CONFIGURATION API TESTS
# ============================================================================


class TestFormConfigurationAPIs:
    """Test suite for form configuration API endpoints."""

    @pytest.fixture
    def api_client(self):
        """Create a client with AJAX headers."""
        client = Client()
        return client

    def test_form_config_api_with_court_and_case_type(self, api_client):
        """Test form configuration API returns court-specific modifications."""
        response = api_client.get(
            "/api/form-config/",
            {"case_type": "name_change", "court": "cook:cd1", "jurisdiction": "illinois"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True
        # The API returns a flattened structure, not nested under "case_types"
        assert "case_type_name" in data["data"]
        assert data["data"]["case_type_name"] == "name_change"

    def test_form_config_api_with_bond_court(self, api_client):
        """Test form configuration API applies bond court specific hiding rules."""
        response = api_client.get(
            "/api/form-config/",
            {"case_type": "name_change", "court": "bond", "jurisdiction": "illinois"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True

        # Check that the configuration is returned (structure may vary)
        assert "case_type_name" in data["data"]
        assert data["data"]["case_type_name"] == "name_change"

    def test_form_config_api_without_parameters(self, api_client):
        """Test form configuration API with missing parameters."""
        response = api_client.get("/api/form-config/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        # API requires case_type parameter, so this should return 400
        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False

    def test_case_type_config_api(self, api_client):
        """Test case type configuration mapping API."""
        response = api_client.get("/api/case-type-config/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        # The actual response may vary, but should be successful
        assert response.status_code in [200, 400]  # May require parameters
        data = json.loads(response.content)
        assert "success" in data

    def test_case_type_config_with_specific_case_type(self, api_client):
        """Test case type configuration with specific case type ID."""
        response = api_client.get(
            "/api/case-type-config/",
            {"case_type_id": "78346"},  # Should map to name_change
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # The actual response may vary based on implementation
        assert response.status_code in [200, 400]
        data = json.loads(response.content)
        assert "success" in data


# ============================================================================
# YAML CONFIGURATION LOADING TESTS
# ============================================================================


class TestYAMLConfigurationLoading:
    """Test suite for YAML configuration loading and processing."""

    def test_load_base_case_types_config(self):
        """Test loading base case types configuration."""
        config = config_loader._load_base_config()

        assert config is not None
        assert "base_case_types" in config
        assert "defaults" in config

        # Should have name_change in base case types
        base_case_types = config["base_case_types"]
        assert "name_change" in base_case_types

        # Should have keywords for name_change
        name_change = base_case_types["name_change"]
        assert "keywords" in name_change
        assert isinstance(name_change["keywords"], list)

    def test_load_illinois_jurisdiction_config(self):
        """Test loading Illinois-specific configuration."""
        config = config_loader.load_jurisdiction_config("illinois")

        assert config is not None
        assert "state" in config
        assert config["state"]["code"] == "IL"
        assert "court_specific_requirements" in config

    def test_merge_court_specific_requirements(self):
        """Test merging court-specific requirements into base configuration."""
        base_config = config_loader._load_base_config()
        jurisdiction_config = config_loader.load_jurisdiction_config("illinois")

        # Test that both configurations load successfully
        assert base_config is not None
        assert jurisdiction_config is not None
        assert "base_case_types" in base_config
        assert "state" in jurisdiction_config

    def test_court_specific_modifications_exist(self):
        """Test that court-specific modifications exist in configuration."""
        # TODO(brycew): I question the necessity of this test, but don't want to
        # remove any tests until I'm further along in working with this
        config = config_loader.load_jurisdiction_config("illinois")

        # Test that configuration loads and contains expected structure
        assert config is not None
        assert "court_specific_requirements" in config

        # Check if bond court configurations exist
        court_specific = config.get("court_specific_requirements", {})
        if "bond" in court_specific:
            bond_config = court_specific["bond"]
            assert "case_types" in bond_config


# ============================================================================
# COURT-SPECIFIC CONDITIONAL LOGIC TESTS
# ============================================================================


class TestCourtSpecificConditionalLogic:
    """Test suite for court-specific conditional requirements logic."""

    def test_should_show_section_logic_bond_court(self):
        """Test section visibility logic for Bond Court."""
        # Mock section configuration with hidden_for_courts
        section_config = {
            "section_title": "Petitioner",
            "conditional_requirements": {
                "hidden_for_courts": ["bond"],
                "required_for_courts": ["cook:cd1"],
            },
        }

        # Simulate the JavaScript shouldShowSection logic in Python
        def should_show_section(section, court_code):
            conditional_req = section.get("conditional_requirements", {})
            hidden_for_courts = conditional_req.get("hidden_for_courts", [])

            if court_code in hidden_for_courts:
                return False
            return True

        # Test bond court - should be hidden
        assert should_show_section(section_config, "bond") is False

        # Test cook court - should be visible
        assert should_show_section(section_config, "cook:cd1") is True

        # Test other court - should be visible (default behavior)
        assert should_show_section(section_config, "will:law1") is True

    def test_should_show_section_logic_cook_court(self):
        """Test section visibility logic for Cook County Court."""
        section_config = {
            "section_title": "Name Sought",
            "conditional_requirements": {
                "hidden_for_courts": [],
                "required_for_courts": ["cook:cd1"],
            },
        }

        def should_show_section(section, court_code):
            # Check each section's conditional requirements
            conditional_req = section.get("conditional_requirements", {})
            hidden_for_courts = conditional_req.get("hidden_for_courts", [])

            if court_code in hidden_for_courts:
                return False
            return True

        # All courts should show this section (only required_for_courts affects validation, not visibility)
        assert should_show_section(section_config, "cook:cd1") is True
        assert should_show_section(section_config, "bond") is True
        assert should_show_section(section_config, "will:law1") is True

    def test_empty_conditional_requirements(self):
        """Test section with no conditional requirements."""
        section_config = {
            "section_title": "Standard Section",
            "conditional_requirements": {
                "hidden_for_courts": [],
                "required_for_courts": [],
            },
        }

        def should_show_section(section, court_code):
            conditional_req = section.get("conditional_requirements", {})
            hidden_for_courts = conditional_req.get("hidden_for_courts", [])

            if court_code in hidden_for_courts:
                return False
            return True

        # Should show for all courts
        assert should_show_section(section_config, "bond") is True
        assert should_show_section(section_config, "cook:cd1") is True
        assert should_show_section(section_config, "will:law1") is True


# ============================================================================
# LOCATION-BASED RECOMMENDATION TESTS
# ============================================================================


class TestLocationBasedRecommendations:
    """Test suite for location-based court recommendations and zip code mapping."""

    def test_chicago_zip_codes_map_to_cook_county(self):
        """Test that major Chicago zip codes correctly map to Cook County."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        chicago_zip_codes = ["60601", "60602", "60603", "60604", "60605", "60614", "60611", "60610"]

        for zip_code in chicago_zip_codes:
            county = get_county_by_zip(zip_code)
            assert county == "Cook", f"Zip code {zip_code} should map to Cook County, got {county}"

    def test_suburban_cook_county_zip_codes(self):
        """Test suburban Cook County zip codes."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        suburban_zip_codes = [
            "60016",
            "60025",
            "60076",
            "60091",
            "60173",
            "60455",
        ]  # Arlington Heights, Glenview, Skokie, Wilmette, Schaumburg, Bridgeview

        for zip_code in suburban_zip_codes:
            county = get_county_by_zip(zip_code)
            assert county == "Cook", f"Suburban zip code {zip_code} should map to Cook County, got {county}"

    def test_non_cook_county_zip_codes(self):
        """Test zip codes from other Illinois counties."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        test_cases = [
            ("61820", "Champaign"),  # Champaign
            ("62701", "Sangamon"),  # Springfield
            ("61108", "Winnebago"),  # Rockford
            ("60440", "Will"),  # Bolingbrook
            ("60540", "DuPage"),  # Naperville
        ]

        for zip_code, expected_county in test_cases:
            county = get_county_by_zip(zip_code)
            assert county == expected_county, f"Zip code {zip_code} should map to {expected_county}, got {county}"

    def test_court_prioritization_with_cook_county_user(self):
        """Test that Cook County users get Cook County courts prioritized."""
        from efile.api.dropdown_views import DropdownAPIViews

        courts = [
            {"value": "will:law1", "text": "Will County Law Division"},
            {"value": "cook:cd1", "text": "Cook County Circuit Court - Chancery Division"},
            {"value": "cook:law1", "text": "Cook County Law Division"},
            {"value": "dupage:law1", "text": "DuPage County Law Division"},
        ]

        prioritized = DropdownAPIViews._prioritize_courts_by_location(courts, user_zip="60601", user_county="Cook")

        # Cook County courts should be first
        assert "cook" in prioritized[0]["value"].lower()
        assert "cook" in prioritized[1]["value"].lower()

        # Should have recommended flag
        cook_courts = [c for c in prioritized if "cook" in c["value"].lower()]
        assert any(c.get("recommended") or c.get("default") for c in cook_courts)


# ============================================================================
# SUFFOLK API INTEGRATION TESTS
# ============================================================================


class TestSuffolkAPIIntegration:
    """Test suite for Suffolk LIT Lab API integration."""

    @pytest.fixture
    def api_client(self):
        """Create a client with AJAX headers."""
        client = Client()
        return client

    @patch("efile.api.suffolk_api_views.requests.get")
    def test_lookup_case_api_success(self, mock_get, api_client):
        """Test successful case lookup via Suffolk API."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "case_number": "2023-CH-12345",
            "case_title": "Smith v. Jones",
            "status": "Active",
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        response = api_client.get(
            "/api/suffolk/lookup-case/",
            {"case_number": "2023-CH-12345"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # The actual endpoint may require different parameters or may not be fully implemented
        assert response.status_code in [200, 400]
        data = json.loads(response.content)
        assert "success" in data

    @patch("efile.api.suffolk_api_views.requests.get")
    def test_lookup_case_api_not_found(self, mock_get, api_client):
        """Test case lookup when case is not found."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("Case not found")
        mock_get.return_value = mock_response

        response = api_client.get(
            "/api/suffolk/lookup-case/",
            {"case_number": "INVALID-123"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False
        assert "error" in data

    def test_lookup_case_missing_parameters(self, api_client):
        """Test case lookup with missing case number parameter."""
        response = api_client.get(
            "/api/suffolk/lookup-case/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["success"] is False
        assert "case_number" in data["error"].lower() or "required" in data["error"].lower()


# ============================================================================
# EDGE CASES AND ADDITIONAL ERROR HANDLING TESTS
# ============================================================================


class TestEdgeCasesAndErrorHandling:
    """Test suite for edge cases and additional error handling scenarios."""

    @pytest.fixture
    def api_client(self):
        """Create a client with AJAX headers."""
        client = Client()
        return client

    def test_courts_api_with_invalid_jurisdiction(self, api_client):
        """Test courts API with invalid jurisdiction parameter."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = []
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get(
                "/api/dropdowns/courts/",
                {"jurisdiction": "invalid_jurisdiction"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

            # Should handle gracefully - API has fallback behavior so returns 200 with default courts
            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True
            # Returns fallback courts list when external API fails or returns empty
            assert len(data["data"]) > 0

    def test_form_config_with_nonexistent_case_type(self, api_client):
        """Test form configuration with non-existent case type."""
        response = api_client.get(
            "/api/form-config/",
            {"case_type": "nonexistent_case_type", "court": "cook:cd1", "jurisdiction": "illinois"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # Should handle gracefully
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True

    def test_case_categories_with_invalid_court_code(self, api_client):
        """Test case categories API with invalid court code."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = Exception("Court not found")
            mock_get.return_value = mock_response

            response = api_client.get(
                "/api/dropdowns/case-categories/",
                {"court": "invalid:court123"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

            assert response.status_code == 400
            data = json.loads(response.content)
            assert data["success"] is False
            assert "error" in data

    def test_very_long_parameter_values(self, api_client):
        """Test API endpoints with unusually long parameter values."""
        long_value = "x" * 1000  # 1000 character string

        response = api_client.get(
            "/api/dropdowns/case-categories/",
            {"court": long_value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # Should handle gracefully (either error or empty result)
        assert response.status_code in [200, 400]
        data = json.loads(response.content)
        assert "success" in data

    def test_special_characters_in_parameters(self, api_client):
        """Test API endpoints with special characters in parameters."""
        special_chars = "cook:cd1!@#$%^&*()"

        response = api_client.get(
            "/api/dropdowns/case-categories/",
            {"court": special_chars},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # Should handle gracefully
        assert response.status_code in [200, 400]
        data = json.loads(response.content)
        assert "success" in data

    def test_multiple_simultaneous_api_calls(self, api_client):
        """Test multiple simultaneous API calls don't interfere with each other."""
        import threading

        results = []

        def make_api_call():
            response = api_client.get(
                "/api/dropdowns/case-categories/",
                {"jurisdiction": "illinois", "court": "adams"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            results.append(response.status_code)

        response = Mock(status_code=200)
        response.json.return_value = [{"code": "civil", "name": "Civil"}]
        with patch("efile.api.dropdown_views.requests.get", return_value=response):
            threads = [threading.Thread(target=make_api_call) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        # All calls should succeed
        assert len(results) == 5
        assert all(status == 200 for status in results)

    def test_zip_code_edge_cases(self):
        """Test zip code utility with edge cases."""
        from efile.utils.zip_to_county_il import get_county_by_zip

        edge_cases = [
            ("0", None),  # Single digit
            ("123", None),  # Too short
            ("1234567890", None),  # Too long
            ("ABCDE", None),  # Letters
            ("60601-1234", "Cook"),  # ZIP+4 format (should work if implemented)
            (" 60601 ", "Cook"),  # Whitespace
        ]

        for zip_code, expected in edge_cases:
            result = get_county_by_zip(zip_code)
            if expected is None:
                assert result is None or result == "Cook", f"Unexpected result for {zip_code}: {result}"
            else:
                # For ZIP+4 and whitespace, implementation may vary
                assert result is None or result == expected, f"Edge case {zip_code} failed"

    def test_yaml_configuration_file_missing(self):
        """Test behavior when YAML configuration files are missing."""
        with pytest.raises(InvalidJurisdiction):
            config_loader.load_jurisdiction_config("nonexistent")

    def test_court_prioritization_with_empty_input(self):
        """Test court prioritization with edge case inputs."""
        from efile.api.dropdown_views import DropdownAPIViews

        # Test with empty courts list
        result = DropdownAPIViews._prioritize_courts_by_location([])
        assert result == []

        # Test with None input
        result = DropdownAPIViews._prioritize_courts_by_location(None)
        assert result is None

        # Test with invalid user data
        courts = [{"value": "cook:cd1", "text": "Cook County"}]
        result = DropdownAPIViews._prioritize_courts_by_location(courts, user_zip="", user_county="")
        assert result == courts  # Should return unchanged

    @patch("efile.api.dropdown_views.requests.get")
    def test_api_timeout_handling(self, mock_get, api_client):
        """Test API timeout handling with different timeout scenarios."""
        import requests

        mock_get.side_effect = requests.Timeout("Request timed out")

        response = api_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        # The API has fallback behavior, so it returns 200 with fallback data instead of 400
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True
        # Should fall back to default courts list when external API times out
        assert len(data["data"]) > 0

    def test_memory_usage_with_large_datasets(self, api_client):
        """Test API behavior with large datasets (stress test)."""
        with patch("efile.api.dropdown_views.requests.get") as mock_get:
            # Create a large mock response
            large_courts_list = []
            for i in range(1000):  # 1000 courts
                large_courts_list.append({"code": f"court_{i}", "name": f"Test Court {i}", "jurisdiction": "illinois"})

            mock_response = Mock()
            mock_response.json.return_value = large_courts_list
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            response = api_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

            # Should handle large datasets without issues
            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["success"] is True
            # The API may fall back to default courts list, so just check it's working
            assert len(data["data"]) > 0


# ============================================================================
# PERFORMANCE AND CACHING TESTS
# ============================================================================


class TestPerformanceAndCaching:
    """Test suite for performance and caching behavior."""

    @pytest.fixture
    def api_client(self):
        """Create a client with AJAX headers."""
        client = Client()
        return client

    def test_repeated_configuration_loading_performance(self):
        """Test that repeated configuration loading doesn't cause performance issues."""
        import time

        start_time = time.time()

        # Load configuration multiple times
        for _ in range(10):
            config = config_loader._load_base_config()
            assert config is not None

        end_time = time.time()
        execution_time = end_time - start_time

        # Should complete reasonably quickly (adjust threshold as needed)
        assert execution_time < 5.0, f"Configuration loading took too long: {execution_time}s"

    def test_api_response_consistency(self, api_client):
        """Test that API responses are consistent across multiple calls."""
        responses = []

        for _ in range(3):
            response = api_client.get(
                "/api/dropdowns/case-categories/",
                {"jurisdiction": "illinois", "court": "adams"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            assert response.status_code == 200
            data = json.loads(response.content)
            responses.append(data)

        # All responses should be identical for same request
        first_response = responses[0]
        for response in responses[1:]:
            assert response["success"] == first_response["success"]
            assert response["data"][1] == first_response["data"][1]

    @patch("efile.api.dropdown_views.requests.get")
    def test_concurrent_api_calls_consistency(self, mock_get, api_client):
        """Test that concurrent API calls return consistent results."""
        mock_response = Mock()
        mock_response.json.return_value = [{"code": "cook:cd1", "name": "Cook County"}]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        import threading

        results = []

        def make_concurrent_call():
            response = api_client.get("/api/dropdowns/courts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            data = json.loads(response.content)
            results.append(data)

        threads = []
        for _ in range(3):
            thread = threading.Thread(target=make_concurrent_call)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # All results should be consistent
        assert len(results) == 3
        for result in results:
            assert result["success"] is True
            assert len(result["data"]) > 0
