"""Adapt the pinned console image to Render without replacing its application.

Fail the build if upstream handler layouts change so no retry path is silently lost.
"""
import ast
from pathlib import Path
import sys


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Pinned image source changed; inspect the Render adaptation")
    return source.replace(old, new)


def patch_handlers(path):
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    counts = {"predicate": 0, "assignment": 0, "transport": 0}

    def replace(node, text):
        edits.append((node.lineno - 1, node.end_lineno, " " * node.col_offset + text + "\n"))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("is_retryable_exception", "_is_retryable_error"):
            argument = node.args.args[0].arg
            edits.append((node.body[0].lineno - 1, node.end_lineno,
                          " " * (node.col_offset + 4) + f"return _render_retry.is_retryable({argument})\n"))
            counts["predicate"] += 1
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "is_retryable"
                                               for target in node.targets):
            expression = ast.unparse(node.value)
            if path.name == "api_helpers.py" and "error_str" in expression:
                replace(node, "is_retryable = _render_retry.is_retryable(e_stream_call)")
            elif path.name == "cookie_proxy.py" and "response.status_code" in expression:
                replace(node, "is_retryable = _render_retry.is_retryable(response)")
            elif path.name == "cookie_proxy.py" and "err_msg" in expression:
                replace(node, "is_retryable = _render_retry.is_retryable(e)")
            else:
                raise RuntimeError(f"Unrecognized retry assignment in {path.name}")
            counts["assignment"] += 1
        if isinstance(node, ast.If) and path.name == "cookie_proxy.py":
            if ast.unparse(node.test) == "(_is_retryable_error(em) or 'timeout' in em.lower()) and attempt < retry_max":
                edits.append((node.lineno - 1, node.lineno,
                              " " * node.col_offset + "if _render_retry.is_retryable(e) and attempt < retry_max:\n"))
                counts["transport"] += 1

    expected = {"predicate": 1, "assignment": 1, "transport": 0} if path.name == "api_helpers.py" else {
        "predicate": 1, "assignment": 3, "transport": 1,
    }
    if counts != expected:
        raise RuntimeError(f"Unexpected retry layout in {path.name}: {counts}")
    for start, end, text in sorted(edits, reverse=True):
        lines[start:end] = [text]
    source = "import render_retry as _render_retry\n" + "".join(lines)
    if path.name == "cookie_proxy.py":
        if source.count("(429, 503, 500)") != 2:
            raise RuntimeError("Cookie HTTP retry layout changed")
        source = source.replace("(429, 503, 500)", "_render_retry.RETRY_STATUS_CODES")
    else:
        source = replace_once(
            source,
            "    return max(0, min(50, retry_max)), max(0.0, min(120.0, backoff))",
            "    return (max(0, min(50, retry_max)) if _render_retry.RETRY_ENABLED else 0), max(0.0, min(120.0, backoff))",
        )
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"Render retry adaptation: {path.name} {counts}")


def patch(root):
    patch_handlers(root / "api_helpers.py")
    patch_handlers(root / "upstreams/cookie_proxy.py")
    config_path = root / "config.py"
    config = config_path.read_text(encoding="utf-8-sig")
    config += "\nimport render_retry as _render_retry\nDEFAULT_SETTINGS.update(retry_max=_render_retry.DEFAULT_RETRY_MAX, retry_backoff_seconds=_render_retry.DEFAULT_BACKOFF)\n"
    compile(config, str(config_path), "exec")
    config_path.write_text(config, encoding="utf-8")

    options_path = root / "http_options.py"
    options = replace_once(options_path.read_text(encoding="utf-8-sig"), "    options = {}\n",
                           "    options = {\"retry_options\": types.HttpRetryOptions(attempts=1)}\n")
    compile(options, str(options_path), "exec")
    options_path.write_text(options, encoding="utf-8")

    main_path = root / "main.py"
    main = main_path.read_text(encoding="utf-8-sig")
    main += '\n\n@app.get("/health")\nasync def render_health():\n    return {"status": "ok", "base_image": "sha256:67c110e477d8d92da5e11034d9f3cdf6cedaec437afabcec64cef26710bea21e"}\n'
    compile(main, str(main_path), "exec")
    main_path.write_text(main, encoding="utf-8")


if __name__ == "__main__":
    patch(Path(sys.argv[1]))
