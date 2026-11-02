"""
Jurisdiction-aware configuration loader for case types and form structures
"""

import logging
import re
from copy import deepcopy
from pathlib import Path

import yaml
from django.conf import settings
from django.core.exceptions import SuspiciousOperation

logger = logging.getLogger(__name__)


class InvalidJurisdiction(SuspiciousOperation, ValueError):
    """Request input must select an installed configuration."""


class JurisdictionConfigLoader:
    """Load and merge YAML configuration files based on jurisdiction"""

    def __init__(self, config_dir=None):
        """
        Args:
            config_dir (str, optional): Path to configuration directory.
                                        Defaults to static/config/.
        """
        if config_dir is None:
            self.config_dir = settings.BASE_DIR / "efile" / "static" / "config"
        else:
            self.config_dir = Path(config_dir)
        self.states_dir = self.config_dir / "states"

        # Make sure directories exist
        self.states_dir.mkdir(parents=True, exist_ok=True)
        # Deployment-owned files define the finite lookup space. Request values
        # must never construct paths or create aliases in the configuration cache.
        self._jurisdiction_paths = {
            path.stem: path
            for path in self.states_dir.glob("*.yaml")
            if re.fullmatch(r"[a-z][a-z0-9_-]*", path.stem) and path.is_file() and not path.is_symlink()
        }

        self.base_config = self._load_base_config()
        # Using in object cache because `@lru_cache` can cause
        # memory leaks when used on objects.
        self._jurisdiction_cache = {}
        self._jurisdiction_versions = {}

    def _load_base_config(self):
        """Load the base configuration file"""
        base_path = self.config_dir / "base-case-types.yaml"
        try:
            with open(base_path) as f:
                return yaml.safe_load(f)
        except FileNotFoundError as e:
            logger.exception(f"Error loading base-case-types.yaml: {e}")
            return {}

    def get_available_jurisdictions(self):
        """
        Get list of available jurisdiction configurations.

        Returns:
            list: List of available codes
        """
        return sorted(self._jurisdiction_paths)

    def load_jurisdiction_config(self, jurisdiction):
        """
        Load configuration for a specific jurisdiction, merging with base config

        Args:
            jurisdiction (str): The jurisdiction code (e.g., 'illinois', 'massachusetts')

        Returns:
            dict: Merged configuration
        """
        if not isinstance(jurisdiction, str) or jurisdiction not in self._jurisdiction_paths:
            raise InvalidJurisdiction("Unknown jurisdiction")
        jurisdiction_path = self._jurisdiction_paths[jurisdiction]
        try:
            stat = jurisdiction_path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
            if jurisdiction in self._jurisdiction_cache and self._jurisdiction_versions.get(jurisdiction) == version:
                return self._jurisdiction_cache[jurisdiction]
            with open(jurisdiction_path) as f:
                jurisdiction_config = yaml.safe_load(f)

            # Merge with base configuration
            merged = JurisdictionConfigLoader._deep_merge(self.base_config, jurisdiction_config)
            if not merged.get("case_types"):
                merged["case_types"] = {}
            for case_name, case_config in merged["case_types"].items():
                if "extends" in case_config:
                    extends_ref = case_config["extends"]
                    base = None

                    # Parse the extends reference (e.g., "base_case_types.name_change")
                    if "." in extends_ref:
                        section, key = extends_ref.split(".", 1)
                        if section in merged and key in merged[section]:
                            base = merged[section][key]

                    if base:
                        # Deep merge base config with the current config
                        merged["case_types"][case_name] = JurisdictionConfigLoader._deep_merge(base, case_config)
            self._jurisdiction_cache[jurisdiction] = merged
            self._jurisdiction_versions[jurisdiction] = version
            return merged

        except FileNotFoundError:
            raise InvalidJurisdiction("Jurisdiction configuration is unavailable") from None

    @staticmethod
    def _deep_merge(base, overlay):
        """
        Deep merge two dictionaries, with overlay taking precedence

        Args:
            base (dict): Base configuration
            overlay (dict): Configuration to overlay

        Returns:
            dict: Merged configuration
        """
        if not isinstance(base, dict) or not isinstance(overlay, dict):
            return overlay

        result = deepcopy(base)

        for key, value in overlay.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = JurisdictionConfigLoader._deep_merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    def get_short_jurisdiction_config(self, jurisdiction):
        return self.load_jurisdiction_config(jurisdiction)["jurisdiction"]

    def get_upload_disclaimers(self, jurisdiction):
        """Statewide advance notices; independent of court selection and acceptance."""
        return deepcopy(self.load_jurisdiction_config(jurisdiction).get("upload_disclaimers") or [])

    def document_checklists_enabled(self, jurisdiction):
        """Whether this jurisdiction currently offers checklists and filing plans."""
        return (self.load_jurisdiction_config(jurisdiction) or {}).get("document_checklists_enabled", True) is not False

    def get_document_checklist_config(self, jurisdiction, court=None):
        """
        Get the config sections that carry partner document checklists.

        Case types and case categories are returned separately because a
        checklist is matched against the court's own case type name first and
        its case category name only as a fallback. Court entries in
        ``court_specific_requirements`` are deep merged in, so one court can
        change a single checklist item, or add its own local form, without
        restating the rest of the list.

        Args:
            jurisdiction (str): The jurisdiction code
            court (str, optional): Court code whose overrides should be applied

        Returns:
            dict: {"case_types": {...}, "case_categories": {...}}
        """
        jurisdiction_config = self.load_jurisdiction_config(jurisdiction) or {}

        # A state's own case_types (already merged with whatever they extend)
        # layer on top of the base case types. Nothing shipped in the base file
        # carries a checklist -- required forms differ too much from state to
        # state for a national default to be safe -- but the merge stays so a
        # state case type keeps whatever it extends.
        case_types = JurisdictionConfigLoader._deep_merge(
            jurisdiction_config.get("base_case_types") or {},
            jurisdiction_config.get("case_types") or {},
        )
        sections = {
            "case_types": case_types,
            "case_categories": deepcopy(jurisdiction_config.get("case_categories") or {}),
        }

        court_requirements = (jurisdiction_config.get("court_specific_requirements") or {}).get(court or "", {})
        for section_name, section in sections.items():
            overrides = court_requirements.get(section_name) or {}
            for key, override in overrides.items():
                if not isinstance(override, dict):
                    continue
                section[key] = JurisdictionConfigLoader._deep_merge(section.get(key, {}), override)

        return sections

    def _find_with_keywords(self, key, cases):
        """
        Given a dictionary with keys and keywords (as a list in the key's value),
        return the value that the key matches (for the dict or the keyword).
        """
        if key in cases:
            return cases[key]

        for case in cases.values():
            if "keywords" in case and key in case["keywords"]:
                return case

        return None

    def get_case_type_config(self, jurisdiction, case_type, court=None):
        """
        Get configuration for a specific case type in a jurisdiction

        Args:
            jurisdiction (str): The jurisdiction code
            case_type (str): The case type identifier
            court (str, optional): Specific court for court-specific requirements

        Returns:
            dict: Case type configuration or None if not found
        """
        jurisdiction = self.load_jurisdiction_config(jurisdiction)

        case_type = case_type.lower()
        case_types_sources = [jurisdiction.get("case_types", {}), jurisdiction.get("base_case_types", {})]

        case_config = None
        for case_types in case_types_sources:
            case_config = self._find_with_keywords(case_type, case_types)
            if case_config:
                break

        if case_config is None:
            if "defaults" in jurisdiction:
                return jurisdiction["defaults"]
            else:
                return None

        case_config = deepcopy(case_config)

        # Apply court-specific requirements if specified
        if court and "court_specific_requirements" in jurisdiction:
            court_requirements = jurisdiction["court_specific_requirements"].get(court, {})
            if "case_types" in court_requirements and case_type in court_requirements["case_types"]:
                court_case_specific = court_requirements["case_types"][case_type]
                case_config = JurisdictionConfigLoader._deep_merge(case_config, court_case_specific)
                if "sections" not in case_config:
                    case_config["sections"] = {}
                sections = case_config["sections"]
                # Apply field modifications first
                field_modifications = court_case_specific.get("field_modifications", [])
                for modification in field_modifications:
                    field_group_name = modification.get("field_group")
                    modifications = modification.get("modifications", {})

                    # Apply modifications to the matching field group
                    if "parties" in sections and "fields" in sections["parties"]:
                        # Use a copy of the list to avoid modification during iteration
                        fields_list = sections["parties"]["fields"][:]
                        for field_group in fields_list:
                            if field_group.get("section_title") == field_group_name:
                                # Apply modifications to this field group
                                for key, value in modifications.items():
                                    if key == "hidden" and value:
                                        # Remove this field group entirely if hidden
                                        if field_group in sections["parties"]["fields"]:
                                            sections["parties"]["fields"].remove(field_group)
                                        break
                                    elif key == "required":
                                        field_group["required"] = value
                                    elif key == "conditional_requirements":
                                        field_group["conditional_requirements"] = value
                                break
                # Apply additional fields
                additional_fields = court_case_specific.get("additional_fields", [])
                if additional_fields and "parties" in sections and "fields" in sections["parties"]:
                    # Add additional fields to the first section of parties
                    if sections["parties"]["fields"]:
                        first_section = sections["parties"]["fields"][0]
                        if "fields" in first_section:
                            # Check for existing fields to prevent duplicates
                            existing_field_names = {field.get("name") for field in first_section["fields"]}

                            # Only add fields that don't already exist
                            new_fields = []
                            for field in additional_fields:
                                field_name = field.get("name")
                                if field_name and field_name not in existing_field_names:
                                    new_fields.append(field)

                            if new_fields:
                                first_section["fields"].extend(new_fields)

        return case_config

    # TODO(brycew): get this up to par
    def validate_configuration(self, jurisdiction, case_type):
        """
        Validate that a configuration is properly structured.

        Args:
            jurisdiction (str): jurisdiction code
            case_type (str): Case type

        Returns:
            dict: Validation results with any errors or warnings
        """
        validation_results = {"valid": True, "errors": [], "warnings": []}

        try:
            case_config = self.get_case_type_config(jurisdiction, case_type)

            if not case_config:
                validation_results["valid"] = False
                validation_results["errors"].append(f"No configuration found for {jurisdiction}:{case_type}")
                return validation_results

            # Check for required fields
            if "sections" not in case_config:
                validation_results["errors"].append("Missing 'sections' in case type configuration")
                validation_results["valid"] = False

        except Exception as e:
            validation_results["valid"] = False
            validation_results["errors"].append(f"Configuration validation error: {str(e)}")

        return validation_results


# Global instance
config_loader = JurisdictionConfigLoader()
