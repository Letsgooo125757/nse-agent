"""`nse-agent web` (run the site) and `nse-agent web-setup` (create its secrets)."""
from __future__ import annotations

import getpass
import os
import secrets
import sys

from .security import hash_password


def cmd_web_setup(args) -> int:
    print("Choose the password for the web app (at least 10 characters).")
    while True:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat password: "):
            print("The passwords don't match. Try again.")
            continue
        try:
            hashed = hash_password(pw)
            break
        except ValueError as exc:
            print(exc)
    print("\nAdd these two lines to .env (and to your host's environment settings when deploying).")
    print("Keep the single quotes: the hash contains $ signs.\n")
    print(f"APP_PASSWORD_HASH='{hashed}'")
    print(f"SESSION_SECRET='{secrets.token_urlsafe(48)}'")
    return 0


def cmd_web(args) -> int:
    import uvicorn

    local = args.host in ("127.0.0.1", "localhost")
    if local:
        os.environ.setdefault("COOKIE_SECURE", "false")   # plain http on your own machine
    try:
        from .app import WebSettings
        WebSettings.from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    where = f"http://{args.host}:{args.port}"
    print(f"NSE desk running at {where}  (Ctrl+C to stop)")
    uvicorn.run("nse_agent.web.app:app", host=args.host, port=args.port, reload=args.reload,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1" if local else "*", log_level="warning")
    return 0


def register(sub) -> None:
    p = sub.add_parser("web", help="run the web app (dashboard + assistant)")
    p.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = only this computer (default)")
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    p.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    p.set_defaults(func=cmd_web)
    sub.add_parser("web-setup", help="create the web app password hash and session secret") \
        .set_defaults(func=cmd_web_setup)
