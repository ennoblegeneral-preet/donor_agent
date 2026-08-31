import requests

try:
    import pymongo.errors as pymongo_errors
except ImportError:
    pymongo_errors = None

# Short, sanitized labels safe to show directly in the UI - never the raw
# exception text (which can contain internal URLs, stack traces, or keys).
# Keyed by context so an LLM-call timeout doesn't get mislabeled as a "search"
# failure (the Scoring/Drafting stages never search the web, only call the LLM).
ERROR_LABELS = {
    "auth_error": {"search": "Search API authentication failed (check API key configuration).", "llm": "LLM API authentication failed (check API key configuration)."},
    "rate_limited": {"search": "Search API rate limit reached - try again shortly.", "llm": "LLM API rate limit reached - try again shortly."},
    "timeout": {"search": "Search request timed out.", "llm": "LLM request timed out - the model took too long to respond."},
    "network_error": {"search": "Network error while reaching the search service.", "llm": "Network error while reaching the LLM service."},
    "blocked": {"search": "The source website blocked our request.", "llm": "The LLM service blocked our request."},
    "server_error": {"search": "The external service returned a server error.", "llm": "The LLM service returned a server error."},
    "db_error": {"search": "Database is unreachable.", "llm": "Database is unreachable."},
    "api_error": {"search": "A search/API service error occurred.", "llm": "An LLM API service error occurred."},
    "no_data": {"search": "No information was found for this company.", "llm": "No information was found for this company."},
}


def classify_error(exc: Exception, context: str = "search") -> dict:
    """
    Kisi bhi exception ko ek chhoti, sanitized category + safe user-facing
    message mein map karta hai, taaki frontend ko pata chale ki "Not Found"
    asal mein ek search/API/DB failure thi, ya genuinely koi data nahi mila.

    context: "search" (default, web/Tavily calls) or "llm" (LLM completion
    calls) - only changes which wording is used, not the error type detection.

    Kabhi bhi raw str(exc) frontend ko nahi dikhate - wo internal URLs/keys
    leak kar sakta hai.
    """
    if pymongo_errors is not None and isinstance(exc, pymongo_errors.PyMongoError):
        error_type = "db_error"
    elif isinstance(exc, requests.exceptions.Timeout):
        error_type = "timeout"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        error_type = "network_error"
    elif isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            error_type = "blocked"
        elif status == 429:
            error_type = "rate_limited"
        elif status is not None and status >= 500:
            error_type = "server_error"
        else:
            error_type = "api_error"
    else:
        text = str(exc).lower()
        if "api key" in text or "unauthorized" in text or "401" in text:
            error_type = "auth_error"
        elif "429" in text or "rate limit" in text:
            error_type = "rate_limited"
        elif "timeout" in text or "timed out" in text:
            error_type = "timeout"
        elif "forbidden" in text or "403" in text or "blocked" in text:
            error_type = "blocked"
        else:
            error_type = "api_error"

    return {"type": error_type, "message": ERROR_LABELS[error_type][context]}


def no_data_error() -> dict:
    """Explicit marker for the clean/genuine case: search succeeded, nothing found."""
    return {"type": "no_data", "message": ERROR_LABELS["no_data"]["search"]}
