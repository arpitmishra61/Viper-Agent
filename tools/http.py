import json
import urllib.error
import urllib.request

_TIMEOUT = 15
_MAX_BYTES = 100 * 1024  # 100 KB


def http_fetch(url: str, method: str = "GET", headers: dict = None,
               body: str = None, json_body: dict = None) -> str:
    try:
        data = None
        req_headers = {"User-Agent": "pi-agent/1.0"}

        if json_body is not None:
            data = json.dumps(json_body).encode()
            req_headers["Content-Type"] = "application/json"
        elif body:
            data = body.encode()

        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())

        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_MAX_BYTES)
            content_type = resp.headers.get("Content-Type", "")
            status = resp.status

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        trailer = "\n... (truncated at 100 KB)" if len(raw) >= _MAX_BYTES else ""
        return f"HTTP {status}  Content-Type: {content_type}\n\n{text}{trailer}"

    except urllib.error.HTTPError as e:
        body_text = e.read(4096).decode("utf-8", errors="replace")
        return f"HTTP {e.code} {e.reason}\n\n{body_text}"
    except Exception as e:
        return f"Fetch error: {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_fetch",
            "description": (
                "Make an HTTP request and return the response body. "
                "Use this for REST APIs, downloading raw content, or checking URLs — "
                "much lighter than the browser tool. Supports GET, POST, PUT, DELETE, PATCH. "
                "Returns up to 100 KB of response text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                        "description": "HTTP method (default: GET)",
                        "default": "GET",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional HTTP headers as key-value pairs",
                    },
                    "body": {
                        "type": "string",
                        "description": "Raw request body string (for POST/PUT)",
                    },
                    "json_body": {
                        "type": "object",
                        "description": "JSON body — sets Content-Type: application/json automatically",
                    },
                },
                "required": ["url"],
            },
        },
    },
]
