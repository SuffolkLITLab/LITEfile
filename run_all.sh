#!/usr/bin/env bash
set -euo pipefail

# Script location and project paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
APP_DIR="$REPO_ROOT/efile_app"

# Terminal colors (if running in interactive terminal)
if [ -t 1 ]; then
    BOLD="\033[1m"
    GREEN="\033[0;32m"
    BLUE="\033[0;34m"
    YELLOW="\033[0;33m"
    RED="\033[0;31m"
    NC="\033[0m" # No Color
else
    BOLD=""
    GREEN=""
    BLUE=""
    YELLOW=""
    RED=""
    NC=""
fi

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# Default configuration
HOST="127.0.0.1"
PORT="8000"
START_LOCALSTACK=true
STOP_DOCKER_ON_EXIT=false
START_EXTRACTION_WORKER=true
START_CODE_INDEX_WORKER=true
LEGACY_CODE_CRAWL=false
SKIP_SYNC=false
SKIP_MIGRATE=false

show_help() {
    echo -e "${BOLD}Usage:${NC} ./run_all.sh [options]"
    echo ""
    echo "Start the LITEfile application for local development without building Docker containers."
    echo "Automatically sets up a local .venv (using Astral uv), runs database migrations,"
    echo "manages LocalStack for S3 document storage, and starts extraction and filing code search workers."
    echo ""
    echo -e "${BOLD}Options:${NC}"
    echo "  --host <HOST>            Bind address for Django development server (default: 127.0.0.1)"
    echo "  --port <PORT>, -p <PORT> Port for Django development server (default: 8000)"
    echo "  --no-docker              Skip starting LocalStack via Docker"
    echo "  --stop-docker-on-exit    Stop LocalStack container when exiting (default: keep running)"
    echo "  --no-worker              Do not start the document extraction worker"
    echo "  --no-code-index          Do not start the filing code index refresh worker (enabled by default)"
    echo "  --with-code-index        Enable the filing code index worker (default; kept for compatibility)"
    echo "  --legacy-code-crawl      Use the expensive full crawler until the proxy bulk API is deployed"
    echo "  --skip-sync              Skip 'uv sync' check on startup"
    echo "  --skip-migrate           Skip running database migrations on startup"
    echo "  -h, --help               Show this help message and exit"
    echo ""
    echo -e "${BOLD}Examples:${NC}"
    echo "  ./run_all.sh"
    echo "  ./run_all.sh --port 8080"
    echo "  ./run_all.sh --no-docker --no-worker"
    echo "  ./run_all.sh --no-code-index"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port|-p)
            PORT="$2"
            shift 2
            ;;
        --no-docker|--skip-localstack|--no-localstack)
            START_LOCALSTACK=false
            shift
            ;;
        --stop-docker-on-exit)
            STOP_DOCKER_ON_EXIT=true
            shift
            ;;
        --no-worker|--skip-worker)
            START_EXTRACTION_WORKER=false
            shift
            ;;
        --with-code-index)
            START_CODE_INDEX_WORKER=true
            shift
            ;;
        --no-code-index)
            START_CODE_INDEX_WORKER=false
            shift
            ;;
        --legacy-code-crawl)
            LEGACY_CODE_CRAWL=true
            shift
            ;;
        --skip-sync)
            SKIP_SYNC=true
            shift
            ;;
        --skip-migrate)
            SKIP_MIGRATE=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Run './run_all.sh --help' for available options."
            exit 1
            ;;
    esac
done

# 1. Ensure Astral uv is available
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            export PATH="$(dirname "$candidate"):$PATH"
            return 0
        fi
    done

    log_warn "'uv' not found in PATH. Attempting to install via official script..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv >/dev/null 2>&1; then
            log_success "'uv' installed successfully."
            return 0
        fi
    fi

    log_error "Astral 'uv' is required to manage dependencies and virtualenv."
    log_error "Install uv with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
}

ensure_uv

# 2. Check / initialize environment file (.env)
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        log_info "No efile_app/.env found. Creating one from efile_app/.env.example..."
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        log_success "Created efile_app/.env"
    else
        log_warn "Neither efile_app/.env nor efile_app/.env.example found."
    fi
fi

# 3. Create / sync local .venv
if [ "$SKIP_SYNC" = false ]; then
    log_info "Synchronizing dependencies and virtual environment (uv sync --group dev)..."
    uv --directory "$APP_DIR" sync --group dev
    log_success "Virtual environment ready at efile_app/.venv"
fi

# 4. Optional LocalStack (S3 mock) startup via Docker
LOCALSTACK_STARTED=false
if [ "$START_LOCALSTACK" = true ]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        log_info "Starting LocalStack (S3 mock) container..."
        docker compose -f "$REPO_ROOT/compose.yml" up -d localstack
        LOCALSTACK_STARTED=true
        log_success "LocalStack is running on port 4566."

        # Ensure target S3 bucket exists in LocalStack
        uv --directory "$APP_DIR" run python -c "
import os, boto3
from dotenv import load_dotenv
load_dotenv('.env')
endpoint = os.getenv('AWS_S3_ENDPOINT_URL', '')
bucket = os.getenv('AWS_S3_BUCKET_NAME', '')
if endpoint and ('localhost' in endpoint or '127.0.0.1' in endpoint or 'host.docker.internal' in endpoint) and bucket:
    try:
        s3 = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID', 'test'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY', 'test'),
            region_name=os.getenv('AWS_S3_REGION_NAME', 'us-east-1'),
        )
        try:
            s3.head_bucket(Bucket=bucket)
            print(f'LocalStack S3 bucket {bucket} exists')
        except Exception:
            s3.create_bucket(Bucket=bucket)
            print(f'Created LocalStack S3 bucket: {bucket}')
    except Exception as err:
        print(f'LocalStack bucket check warning: {err}')
" 2>/dev/null || true
    else
        log_warn "Docker is not installed or daemon is not running. Skipping LocalStack."
        log_warn "Document uploads require AWS S3 or LocalStack running on port 4566."
    fi
fi

# 5. Run database migrations
if [ "$SKIP_MIGRATE" = false ]; then
    log_info "Applying database migrations..."
    uv --directory "$APP_DIR" run python manage.py migrate --noinput --fake-initial
    log_success "Database migrations up to date."
fi

# 6. Process supervision and cleanup handling
CLEANED_UP=0
cleanup() {
    if [ "$CLEANED_UP" -eq 1 ]; then
        return
    fi
    CLEANED_UP=1
    trap - INT TERM EXIT

    echo ""
    log_info "Stopping application services..."

    # Terminate all background child jobs
    local pids
    pids=$(jobs -p)
    if [ -n "$pids" ]; then
        for pid in $pids; do
            kill -TERM "$pid" 2>/dev/null || true
        done
        # Allow processes up to 1 second to exit gracefully
        sleep 1
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid" 2>/dev/null || true
            fi
        done
        wait 2>/dev/null || true
    fi

    if [ "$LOCALSTACK_STARTED" = true ]; then
        if [ "$STOP_DOCKER_ON_EXIT" = true ]; then
            log_info "Stopping LocalStack container..."
            docker compose -f "$REPO_ROOT/compose.yml" stop localstack 2>/dev/null || true
        else
            log_info "LocalStack container left running in background."
            log_info "To stop it manually: docker compose stop localstack"
        fi
    fi

    log_success "All local dev processes stopped."
    exit 0
}

trap cleanup INT TERM

echo ""
echo -e "${BOLD}======================================================${NC}"
echo -e "${BOLD}${GREEN} LITEfile local development environment is ready!${NC}"
echo -e "${BOLD}======================================================${NC}"
echo -e "  ${BOLD}Web application:${NC} http://${HOST}:${PORT}/"
echo -e "  ${BOLD}Login page:${NC}      http://${HOST}:${PORT}/login"
echo -e "  ${BOLD}Staff portal:${NC}    http://${HOST}:${PORT}/staff-7c83f0a2/"
if [ "$START_EXTRACTION_WORKER" = true ]; then
    echo -e "  ${BOLD}Extraction worker:${NC} ACTIVE (processing PDF lead documents)"
fi
if [ "$START_CODE_INDEX_WORKER" = true ]; then
    echo -e "  ${BOLD}Code index worker:${NC} ENABLED (building or refreshing court filing codes)"
    echo "  Filing code search becomes available after the first successful index build."
fi
if [ "$LOCALSTACK_STARTED" = true ]; then
    echo -e "  ${BOLD}S3 Storage:${NC}        LocalStack (http://127.0.0.1:4566)"
fi
echo -e "${BOLD}======================================================${NC}"
echo -e "${YELLOW}Press Ctrl+C to shut down all processes.${NC}"
echo ""

# 7. Start workers and web server
if [ "$START_EXTRACTION_WORKER" = true ]; then
    log_info "Starting document extraction worker in background..."
    PYTHONUNBUFFERED=1 uv --directory "$APP_DIR" run python manage.py process_document_extractions &
fi

if [ "$START_CODE_INDEX_WORKER" = true ]; then
    log_info "Starting filing code index worker in background..."
    CODE_INDEX_ARGS=(--daily --retry-interval 900 --verbosity 2)
    if [ "$LEGACY_CODE_CRAWL" = true ]; then
        log_warn "Legacy code crawling is enabled: every refresh downloads and rebuilds the full catalog."
        CODE_INDEX_ARGS+=(--legacy-crawl)
    fi
    PYTHONUNBUFFERED=1 uv --directory "$APP_DIR" run python manage.py refresh_filing_code_index "${CODE_INDEX_ARGS[@]}" &
fi

log_info "Starting Django development server at http://${HOST}:${PORT}..."
PYTHONUNBUFFERED=1 uv --directory "$APP_DIR" run python manage.py runserver "${HOST}:${PORT}" &

# Wait for any child job to exit; if any crashes or exits, trigger cleanup
wait || true
cleanup
