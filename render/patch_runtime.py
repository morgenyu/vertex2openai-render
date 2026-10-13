"""Adapt the pinned console image to Render without replacing its application.

Fail the build if upstream handler layouts change so no retry path is silently lost.
"""
import ast
from pathlib import Path
import re
import sys


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Pinned image source changed; inspect the Render adaptation")
    return source.replace(old, new)


def patch_backoff(source, filename):
    lines = source.splitlines(keepends=True)
    loops = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.For) and isinstance(node.target, ast.Name)
             and node.target.id == "attempt"]
    if len(loops) != 3:
        raise RuntimeError(f"Unexpected retry loop count in {filename}: {len(loops)}")
    for node in sorted(loops, key=lambda item: item.lineno, reverse=True):
        if ast.unparse(node.iter) not in ("range(max_retries + 1)", "range(retry_max + 1)"):
            raise RuntimeError(f"Unknown retry loop in {filename}")
        body = "".join(lines[node.lineno:node.end_lineno])
        if not re.search(r"\bbackoff_sec\b", body):
            raise RuntimeError(f"Retry loop has no delay in {filename}")
        body = re.sub(r"\bbackoff_sec\b", "_retry_wait", body)
        indent = " " * (node.col_offset + 4)
        delay = indent + "_retry_wait = _render_retry.retry_delay(attempt, backoff_sec)\n"
        delay += indent + "_proxy_generation = proxy_pool.generation\n"
        lines[node.lineno:node.end_lineno] = [delay, body]
    source = "".join(lines)
    hooks = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Expr)
             and ast.unparse(node.value) == "stats.add_retry()"]
    expected = 3 if filename == "api_helpers.py" else 7
    if len(hooks) != expected:
        raise RuntimeError(f"Unexpected rotation hook count in {filename}: {len(hooks)}")
    lines = source.splitlines(keepends=True)
    for node in sorted(hooks, key=lambda item: item.lineno, reverse=True):
        lines.insert(node.lineno - 1, " " * node.col_offset + "await proxy_pool.rotate(_proxy_generation)\n")
    return "from proxy_pool import proxy_pool\n" + "".join(lines)


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
        source = replace_once(source,
                              '        if app_config.PROXY_URL:\n            client_kwargs["proxy"] = app_config.PROXY_URL\n',
                              '        if app_config.PROXY_URL:\n            client_kwargs["proxy"] = app_config.PROXY_URL\n        if proxy_pool.enabled:\n            client_kwargs["limits"] = httpx.Limits(max_keepalive_connections=0)\n')
    else:
        source = replace_once(
            source,
            "    return max(0, min(50, retry_max)), max(0.0, min(120.0, backoff))",
            "    return (max(0, min(50, retry_max)) if _render_retry.RETRY_ENABLED else 0), max(0.0, min(120.0, backoff))",
        )
    source = patch_backoff(source, path.name)
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
    options = "import httpx\nfrom proxy_pool import proxy_pool\n" + options_path.read_text(encoding="utf-8-sig")
    options = replace_once(options, "    options = {}\n",
                           "    if proxy_pool.enabled:\n        client_args[\"limits\"] = httpx.Limits(max_keepalive_connections=0)\n    options = {\"retry_options\": types.HttpRetryOptions(attempts=1)}\n")
    compile(options, str(options_path), "exec")
    options_path.write_text(options, encoding="utf-8")

    main_path = root / "main.py"
    main = "from proxy_pool import proxy_pool\n" + main_path.read_text(encoding="utf-8-sig")
    main = replace_once(main, "    await refresh_models_config_cache()\n",
                        "    await proxy_pool.start()\n    await refresh_models_config_cache()\n")
    main += '\n\n@app.get("/health")\nasync def render_health():\n    return {"status": "ok", "base_image": "sha256:67c110e477d8d92da5e11034d9f3cdf6cedaec437afabcec64cef26710bea21e"}\n'
    main += '\n\nimport render_egress\n\n@app.get("/api/egress")\nasync def render_egress_status(_auth: bool = Depends(require_auth)):\n    return await render_egress.status()\n\n@app.post("/api/egress/rotate")\nasync def render_rotate_egress(_auth: bool = Depends(require_auth)):\n    return await render_egress.rotate()\n'
    compile(main, str(main_path), "exec")
    main_path.write_text(main, encoding="utf-8")


if __name__ == "__main__":
    patch(Path(sys.argv[1]))
