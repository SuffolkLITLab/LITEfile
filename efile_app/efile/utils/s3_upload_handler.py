"""
S3 Upload utilities for handling document uploads to AWS S3
"""

import logging
import mimetypes
import uuid
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)


class S3UploadHandler:
    """Handles file uploads to AWS S3 and generates secure URLs"""

    def __init__(self):
        """Initialize S3 upload handler with AWS configuration."""
        self.s3_client = None
        self.credentials_checked = False

    def _ensure_initialized(self):
        """Ensure S3 client is initialized - check credentials dynamically."""
        if self.s3_client is not None:
            return True

        # Get credentials from Django settings
        self.access_key_id = getattr(settings, "AWS_ACCESS_KEY_ID", "")
        self.secret_access_key = getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        self.session_token = getattr(settings, "AWS_SESSION_TOKEN", "")
        self.bucket_name = getattr(settings, "AWS_S3_BUCKET_NAME", "")
        self.region_name = getattr(settings, "AWS_S3_REGION_NAME", "us-east-1")
        self.s3_endpoint_url = getattr(settings, "AWS_S3_ENDPOINT_URL", None)

        # Check for required credentials
        if not all([self.access_key_id, self.secret_access_key, self.bucket_name]):
            if not self.credentials_checked:
                logger.warning("AWS credentials not fully configured")
                logger.debug(
                    "AWS credential presence - access_key_id: %s, secret_access_key: %s, bucket_name: %s",
                    "Set" if self.access_key_id else "Missing",
                    "Set" if self.secret_access_key else "Missing",
                    "Set" if self.bucket_name else "Missing",
                )
                self.credentials_checked = True
            return False
        else:
            self._initialize_s3_client()
            return self.s3_client is not None

    def _initialize_s3_client(self):
        """Initialize locally; the requested operation verifies its own access."""
        try:
            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                aws_session_token=self.session_token or None,
                region_name=self.region_name,
                endpoint_url=self.s3_endpoint_url,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=60,
                    max_pool_connections=10,
                    retries={"mode": "standard", "total_max_attempts": 3},
                    s3={"addressing_style": "path" if self.s3_endpoint_url else "virtual"},
                ),
            )

        except Exception:
            logger.error("Document storage initialization failed")
            self.s3_client = None

    def upload_file(self, file_obj, file_type="document", metadata=None):
        """
        Upload a file to S3 and return the URL

        Args:
            file_obj: Django UploadedFile object
            file_type: Type of file (e.g., 'lead_document', 'supporting_document')
            metadata: Optional metadata dictionary

        Returns:
            dict: {
                'success': bool,
                'url': str,
                'key': str,
                'error': str (if failed)
            }
        """
        if not self._ensure_initialized():
            return {"success": False, "error": "S3 client not initialized - check AWS credentials"}

        try:
            # Generate unique filename
            file_extension = self._get_file_extension(file_obj.name)
            unique_filename = f"{uuid.uuid4().hex}{file_extension}"

            # Create S3 key with folder structure
            s3_key = f"efile-documents/{file_type}/{unique_filename}"

            # Prepare metadata
            s3_metadata = {
                "original-filename": quote(file_obj.name),
                "file-type": file_type,
                "upload-timestamp": str(int(uuid.uuid1().time)),
            }

            if metadata:
                s3_metadata.update(metadata)

            # Determine content type
            content_type = file_obj.content_type or mimetypes.guess_type(file_obj.name)[0] or "application/octet-stream"

            # Upload file
            self.s3_client.upload_fileobj(
                file_obj,
                self.bucket_name,
                s3_key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": s3_metadata,
                    # No ACL specified - bucket policy controls access
                },
            )

            # Generate the URL that can be used for efile submission
            file_url = self._generate_file_url(s3_key)

            logger.info("Document upload succeeded")

            return {
                "success": True,
                "url": file_url,
                "key": s3_key,
                "bucket": self.bucket_name,
                "filename": file_obj.name,
                "size": file_obj.size,
                "content_type": content_type,
            }

        except ClientError as e:
            error_msg = f"Failed to upload file to S3: {e}"
            logger.error(f"Document storage request failed: {e}")
            return {"success": False, "error": error_msg}
        except Exception as e:
            error_msg = f"Unexpected error during S3 upload: {e}"
            logger.error(f"Document storage request failed unexpectedly: {e}")
            return {"success": False, "error": error_msg}

    def _generate_file_url(self, s3_key, expiration=3600):
        """
        Generate a presigned URL for the uploaded file

        Args:
            s3_key: S3 object key
            expiration: URL expiration time in seconds (default: 1 hour)

        Returns:
            str: Presigned URL
        """
        try:
            # For efile submission, we might need a longer-lived URL
            # or use a different approach depending on Suffolk's requirements
            presigned_url = self.s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket_name, "Key": s3_key}, ExpiresIn=expiration
            )

            return presigned_url

        except ClientError:
            logger.error("Document URL generation failed")
            raise

    def get_public_url(self, s3_key, expiration=604800):  # 7 days default
        """Get a time-limited URL for a private S3 object."""
        if not self._ensure_initialized():
            raise RuntimeError("S3 client not initialized - check AWS credentials")
        return self.s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket_name, "Key": s3_key}, ExpiresIn=expiration
        )

    def delete_file(self, s3_key):
        """
        Delete a file from S3

        Args:
            s3_key: S3 object key

        Returns:
            dict: {'success': bool, 'error': str (if failed)}
        """
        if not self._ensure_initialized():
            return {"success": False, "error": "S3 client not initialized - check AWS credentials"}

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            logger.info("Document deletion succeeded")
            return {"success": True}

        except ClientError as e:
            error_msg = f"Failed to delete file from S3: {e}"
            logger.error("Document deletion failed")
            return {"success": False, "error": error_msg}

    def erase_file(self, s3_key):
        """Permanently erase this exact key, including noncurrent versions.

        Fail closed when version listing/deletion is denied or object lock blocks
        erasure. Never mistake a delete marker for permanent deletion.
        """
        if not self._ensure_initialized():
            return {"success": False}
        try:
            versioning = self.s3_client.get_bucket_versioning(Bucket=self.bucket_name)
            if versioning.get("Status") in {"Enabled", "Suspended"}:
                objects = []
                pages = self.s3_client.get_paginator("list_object_versions").paginate(
                    Bucket=self.bucket_name, Prefix=s3_key
                )
                for page in pages:
                    objects.extend(
                        {"Key": s3_key, "VersionId": item["VersionId"]}
                        for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
                        if item["Key"] == s3_key
                    )
                for start in range(0, len(objects), 1000):
                    result = self.s3_client.delete_objects(
                        Bucket=self.bucket_name, Delete={"Objects": objects[start : start + 1000], "Quiet": True}
                    )
                    if result.get("Errors"):
                        return {"success": False}
                for page in self.s3_client.get_paginator("list_object_versions").paginate(
                    Bucket=self.bucket_name, Prefix=s3_key
                ):
                    if any(
                        item["Key"] == s3_key for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
                    ):
                        return {"success": False}
            else:
                self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            return {"success": True}
        except Exception:
            logger.warning("Privacy object erasure failed; restricted retry manifest retained")
            return {"success": False}

    def download_file(self, s3_key, destination):
        """Download a private object to a local path for background processing."""
        if not self._ensure_initialized():
            return {"success": False, "error": "S3 client not initialized - check AWS credentials"}

        try:
            self.s3_client.download_file(self.bucket_name, s3_key, destination)
            return {"success": True}
        except ClientError as error:
            error_msg = f"Failed to download file from S3: {error}"
            logger.error("Document download failed")
            return {"success": False, "error": error_msg}

    def _get_file_extension(self, filename):
        """Extract file extension from filename"""
        if "." in filename:
            return "." + filename.rsplit(".", 1)[1].lower()
        return ""

    def validate_file(self, file_obj, max_size_mb=10, allowed_types=None):
        """
        Validate uploaded file

        Args:
            file_obj: Django UploadedFile object
            max_size_mb: Maximum file size in MB
            allowed_types: List of allowed file extensions

        Returns:
            dict: {'valid': bool, 'error': str (if invalid)}
        """
        if allowed_types is None:
            allowed_types = [".pdf", ".doc", ".docx", ".txt"]

        # Check file size
        max_size_bytes = max_size_mb * 1024 * 1024
        if file_obj.size > max_size_bytes:
            return {"valid": False, "error": f"File size exceeds {max_size_mb}MB limit"}

        # Check file type
        file_extension = self._get_file_extension(file_obj.name)
        if file_extension not in allowed_types:
            return {
                "valid": False,
                "error": f"File type {file_extension} not allowed. Allowed types: {', '.join(allowed_types)}",
            }

        # Additional validation for PDF files
        if file_extension == ".pdf":
            # You could add PDF-specific validation here
            pass

        return {"valid": True}
