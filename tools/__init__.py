import tools.search as _search
import tools.calculator as _calculator
import tools.files as _files
import tools.shell as _shell
import tools.python_runner as _python_runner
import tools.browser as _browser
import tools.http as _http
import tools.think as _think

web_search = _search.web_search
calculate = _calculator.calculate
read_file = _files.read_file
write_file = _files.write_file
edit_file = _files.edit_file
list_dir = _files.list_dir
run_shell = _shell.run_shell
run_python = _python_runner.run_python
browser = _browser.browser
http_fetch = _http.http_fetch
think = _think.think

TOOL_DEFINITIONS = (
    _think.DEFINITIONS       # think first — model sees it first in the list
    + _search.DEFINITIONS
    + _calculator.DEFINITIONS
    + _files.DEFINITIONS
    + _shell.DEFINITIONS
    + _python_runner.DEFINITIONS
    + _browser.DEFINITIONS
    + _http.DEFINITIONS
)


def execute_tool(name: str, args: dict) -> str:
    if name == "web_search":
        return web_search(args.get("query", ""), args.get("max_results", 5))
    if name == "calculate":
        return calculate(args.get("expression", ""))
    if name == "read_file":
        return read_file(args.get("path", ""))
    if name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""), args.get("mode", "overwrite"))
    if name == "edit_file":
        return edit_file(args.get("path", ""), args.get("old_string", ""), args.get("new_string", ""))
    if name == "list_dir":
        return list_dir(args.get("path", "."), args.get("pattern", "*"), args.get("recursive", False))
    if name == "run_shell":
        return run_shell(args.get("command", ""), args.get("cwd"), min(args.get("timeout", 60), 300))
    if name == "run_python":
        return run_python(args.get("code", ""), args.get("packages"), min(args.get("timeout", 30), 120))
    if name == "browser":
        return browser(
            action=args.get("action", ""),
            url=args.get("url"),
            text=args.get("text"),
            selector=args.get("selector"),
            value=args.get("value"),
            label=args.get("label"),
            screenshot_path=args.get("screenshot_path"),
            headless=args.get("headless", True),
        )
    if name == "http_fetch":
        return http_fetch(
            url=args.get("url", ""),
            method=args.get("method", "GET"),
            headers=args.get("headers"),
            body=args.get("body"),
            json_body=args.get("json_body"),
        )
    if name == "think":
        return think(args.get("thought", ""))
    return f"Unknown tool: {name}"
