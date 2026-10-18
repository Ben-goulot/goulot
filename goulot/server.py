"""Server start-up: loopback sockets only, port check, daily backup job."""

from __future__ import annotations

import errno
import logging
import os
import socket
from datetime import UTC, datetime, timedelta

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from goulot.api.app import create_app
from goulot.api.context import AppContext, build_context
from goulot.db.backup import backup, last_backup_age
from goulot.pipeline import cycle
from goulot.settings import LOOPBACK_V6, Settings, validate_bind_hosts

log = logging.getLogger("goulot.server")
BACKUP_INTERVAL_HOURS = 24


class PortBusy(RuntimeError):
    pass


def bind_sockets(hosts: tuple[str, ...], port: int) -> list[socket.socket]:
    """Open listening sockets on loopback addresses only. Refuses anything else before opening anything."""
    validate_bind_hosts(hosts)
    sockets: list[socket.socket] = []
    try:
        for host in hosts:
            family = socket.AF_INET6 if host == LOOPBACK_V6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind((host, port))
            except OSError as exc:
                sock.close()
                if exc.errno == errno.EADDRINUSE:
                    raise PortBusy(f"Le port {port} est déjà utilisé : Goulot tourne-t-il déjà ?") from exc
                raise
            sock.listen(64)
            sockets.append(sock)
    except BaseException:
        for s in sockets:
            s.close()
        raise
    return sockets


def _backup_if_due(ctx: AppContext) -> None:
    age = last_backup_age(ctx.settings.backup_dir, ctx.clock.time())
    if age is None or age > (BACKUP_INTERVAL_HOURS - 1) * 3600:
        path = backup(ctx.db, ctx.settings.backup_dir)
        log.info("daily backup written: %s", path.name)


def run(settings: Settings) -> None:
    sockets = bind_sockets(settings.bind_hosts, settings.port)
    ctx = build_context(settings)
    app = create_app(ctx)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_backup_if_due, "interval", hours=1, args=[ctx], id="daily_backup", max_instances=1)
    # Watch cycle: at start-up (catch-up after the Mac slept), then every 3 hours. GOULOT_WATCH=off (development
    # previews only) keeps a server from collecting or calling the AI.
    if os.environ.get("GOULOT_WATCH") != "off":
        scheduler.add_job(cycle.run, "interval", hours=3, args=[ctx], id="watch_cycle", max_instances=1,
                          coalesce=True, next_run_time=datetime.now(UTC) + timedelta(seconds=30))
    else:
        log.warning("watch cycle disabled (GOULOT_WATCH=off)")
    scheduler.start()
    _backup_if_due(ctx)
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        forwarded_allow_ips="",
    )
    try:
        uvicorn.Server(config).run(sockets=sockets)
    finally:
        scheduler.shutdown(wait=False)
        for s in sockets:
            s.close()
