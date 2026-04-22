from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import CheckResult, CheckStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── HTTP Check ─────────────────────────────────────────────────────────────────

async def http_check(
    url: str,
    method: str = "GET",
    expected_status: int = 200,
    headers: dict[str, str] | None = None,
    body_contains: str | None = None,
    latency_warn_ms: float | None = None,
    timeout: float = 10.0,
    name: str = "http",
) -> CheckResult:
    start = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers or {})
        latency_ms = (asyncio.get_event_loop().time() - start) * 1000

        if resp.status_code != expected_status:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                latency_ms=latency_ms,
                error=f"HTTP {resp.status_code} (expected {expected_status})",
                checked_at=_utcnow(),
            )

        if body_contains and body_contains not in resp.text:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                latency_ms=latency_ms,
                error=f"Body missing '{body_contains}'",
                checked_at=_utcnow(),
            )

        status = CheckStatus.UP
        if latency_warn_ms and latency_ms > latency_warn_ms:
            status = CheckStatus.DEGRADED

        return CheckResult(
            name=name,
            status=status,
            latency_ms=latency_ms,
            checked_at=_utcnow(),
        )

    except httpx.TimeoutException:
        latency_ms = (asyncio.get_event_loop().time() - start) * 1000
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            latency_ms=latency_ms,
            error=f"Timeout after {timeout}s",
            checked_at=_utcnow(),
        )
    except Exception as e:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=str(e),
            checked_at=_utcnow(),
        )


# ── TCP Check ──────────────────────────────────────────────────────────────────

async def tcp_check(
    host: str,
    port: int,
    timeout: float = 10.0,
    name: str = "tcp",
) -> CheckResult:
    start = asyncio.get_event_loop().time()
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        latency_ms = (asyncio.get_event_loop().time() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return CheckResult(
            name=name,
            status=CheckStatus.UP,
            latency_ms=latency_ms,
            checked_at=_utcnow(),
        )
    except asyncio.TimeoutError:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=f"TCP timeout after {timeout}s connecting to {host}:{port}",
            checked_at=_utcnow(),
        )
    except (ConnectionRefusedError, OSError) as e:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=f"Connection refused: {e}",
            checked_at=_utcnow(),
        )
    except Exception as e:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=str(e),
            checked_at=_utcnow(),
        )


# ── Docker Check ───────────────────────────────────────────────────────────────

async def docker_check(
    container_name: str,
    check_health: bool = True,
    name: str = "docker",
) -> CheckResult:
    try:
        import docker  # type: ignore
    except ImportError:
        return CheckResult(
            name=name,
            status=CheckStatus.UNKNOWN,
            error="docker SDK not installed",
            checked_at=_utcnow(),
        )

    try:
        client = docker.from_env()
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                error=f"Container '{container_name}' not found",
                checked_at=_utcnow(),
            )

        state = container.attrs.get("State", {})
        running = state.get("Running", False)

        if not running:
            status_str = state.get("Status", "unknown")
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                error=f"Container status: {status_str}",
                details={"container_status": status_str},
                checked_at=_utcnow(),
            )

        if check_health:
            health = state.get("Health", {})
            health_status = health.get("Status", "")
            if health_status == "unhealthy":
                last_log = ""
                logs = health.get("Log", [])
                if logs:
                    last_log = logs[-1].get("Output", "")
                return CheckResult(
                    name=name,
                    status=CheckStatus.DOWN,
                    error=f"Container unhealthy: {last_log.strip()[:200]}",
                    details={"health_status": health_status},
                    checked_at=_utcnow(),
                )
            if health_status == "starting":
                return CheckResult(
                    name=name,
                    status=CheckStatus.DEGRADED,
                    error="Container health: starting",
                    checked_at=_utcnow(),
                )

        return CheckResult(
            name=name,
            status=CheckStatus.UP,
            details={"container_status": state.get("Status", "running")},
            checked_at=_utcnow(),
        )

    except Exception as e:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=f"Docker error: {e}",
            checked_at=_utcnow(),
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


# ── API Custom Check ───────────────────────────────────────────────────────────

def _resolve_field(data: Any, field_path: str) -> tuple[bool, Any]:
    """Navigate nested dict/list using dot notation. Returns (exists, value)."""
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def _apply_operator(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "neq":
        return actual != expected
    if operator == "contains":
        return expected in actual
    if operator == "gt":
        return actual > expected
    if operator == "lt":
        return actual < expected
    if operator == "exists":
        return True  # field was found
    return False


# ── Host Metrics Check ─────────────────────────────────────────────────────────

def _read_cpu_temp() -> float | None:
    """Read CPU temperature. Tries psutil first, then /sys/class/thermal fallback."""
    try:
        import psutil
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if key in temps and temps[key]:
                return float(temps[key][0].current)
    except (AttributeError, OSError, ImportError):
        pass
    # Fallback: read from bind-mounted host /sys or container /sys
    for base in ("/host-sys", "/sys"):
        try:
            with open(f"{base}/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except (FileNotFoundError, ValueError, OSError):
            continue
    return None


async def host_metrics_check(
    mounts: list[str] | None = None,
    cpu_warn: float = 80.0,
    cpu_crit: float = 95.0,
    ram_warn: float = 85.0,
    ram_crit: float = 95.0,
    disk_warn: float = 85.0,
    disk_crit: float = 95.0,
    temp_warn: float | None = 75.0,
    temp_crit: float | None = 90.0,
    name: str = "host_metrics",
) -> CheckResult:
    """Read host CPU / RAM / disk usage via psutil.

    When running in Docker, psutil reads the container's /proc unless PROCFS_PATH
    is redirected to a bind-mounted host /proc. Set env var HOST_PROC or mount
    host /proc to /host-proc in the container.
    """
    import os
    try:
        import psutil
    except ImportError:
        return CheckResult(
            name=name,
            status=CheckStatus.UNKNOWN,
            error="psutil not installed",
            checked_at=_utcnow(),
        )

    # Redirect psutil to host procfs if bind-mounted
    host_proc = os.environ.get("HOST_PROC", "")
    if host_proc and os.path.isdir(host_proc):
        psutil.PROCFS_PATH = host_proc

    issues_warn: list[str] = []
    issues_crit: list[str] = []
    details: dict[str, Any] = {}

    try:
        cpu = psutil.cpu_percent(interval=0.5)
        details["cpu_percent"] = round(cpu, 1)
        if cpu >= cpu_crit:
            issues_crit.append(f"CPU {cpu:.1f}% ≥ {cpu_crit}%")
        elif cpu >= cpu_warn:
            issues_warn.append(f"CPU {cpu:.1f}% ≥ {cpu_warn}%")
    except Exception as e:
        issues_warn.append(f"CPU lecture échouée: {e}")

    try:
        vm = psutil.virtual_memory()
        details["ram_percent"] = round(vm.percent, 1)
        details["ram_used_gb"] = round(vm.used / 1024**3, 2)
        details["ram_total_gb"] = round(vm.total / 1024**3, 2)
        if vm.percent >= ram_crit:
            issues_crit.append(f"RAM {vm.percent:.1f}% ≥ {ram_crit}%")
        elif vm.percent >= ram_warn:
            issues_warn.append(f"RAM {vm.percent:.1f}% ≥ {ram_warn}%")
    except Exception as e:
        issues_warn.append(f"RAM lecture échouée: {e}")

    cpu_temp = _read_cpu_temp()
    if cpu_temp is not None:
        details["cpu_temp_celsius"] = round(cpu_temp, 1)
        if temp_crit is not None and cpu_temp >= temp_crit:
            issues_crit.append(f"CPU temp {cpu_temp:.1f}°C ≥ {temp_crit}°C")
        elif temp_warn is not None and cpu_temp >= temp_warn:
            issues_warn.append(f"CPU temp {cpu_temp:.1f}°C ≥ {temp_warn}°C")

    disk_results = []
    for mount in (mounts or ["/host-rootfs"]):
        try:
            usage = psutil.disk_usage(mount)
            entry = {
                "mount": mount,
                "percent": round(usage.percent, 1),
                "used_gb": round(usage.used / 1024**3, 2),
                "total_gb": round(usage.total / 1024**3, 2),
            }
            disk_results.append(entry)
            if usage.percent >= disk_crit:
                issues_crit.append(f"Disque {mount} {usage.percent:.1f}% ≥ {disk_crit}%")
            elif usage.percent >= disk_warn:
                issues_warn.append(f"Disque {mount} {usage.percent:.1f}% ≥ {disk_warn}%")
        except (FileNotFoundError, OSError) as e:
            issues_warn.append(f"Disque {mount} lecture échouée: {e}")
    details["disks"] = disk_results

    if issues_crit:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error="; ".join(issues_crit + issues_warn),
            details=details,
            checked_at=_utcnow(),
        )
    if issues_warn:
        return CheckResult(
            name=name,
            status=CheckStatus.DEGRADED,
            error="; ".join(issues_warn),
            details=details,
            checked_at=_utcnow(),
        )
    return CheckResult(
        name=name,
        status=CheckStatus.UP,
        details=details,
        checked_at=_utcnow(),
    )


async def api_custom_check(
    url: str,
    validations: list[dict],
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    name: str = "api_custom",
) -> CheckResult:
    start = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers or {})
        latency_ms = (asyncio.get_event_loop().time() - start) * 1000

        if resp.status_code >= 400:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                latency_ms=latency_ms,
                error=f"HTTP {resp.status_code}",
                checked_at=_utcnow(),
            )

        try:
            body = resp.json()
        except Exception:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                latency_ms=latency_ms,
                error="Response is not valid JSON",
                checked_at=_utcnow(),
            )

        failures = []
        for v in validations:
            field = v["field"]
            operator = v["operator"]
            expected = v.get("value")
            failure_message = v.get("failure_message", f"Validation failed: {field} {operator} {expected}")

            exists, actual = _resolve_field(body, field)
            if not exists:
                if operator == "exists":
                    failures.append(f"Field '{field}' not found")
                else:
                    failures.append(failure_message)
                continue

            if not _apply_operator(operator, actual, expected):
                failures.append(failure_message)

        if failures:
            return CheckResult(
                name=name,
                status=CheckStatus.DOWN,
                latency_ms=latency_ms,
                error="; ".join(failures),
                checked_at=_utcnow(),
            )

        return CheckResult(
            name=name,
            status=CheckStatus.UP,
            latency_ms=latency_ms,
            checked_at=_utcnow(),
        )

    except httpx.TimeoutException:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=f"Timeout after {timeout}s",
            checked_at=_utcnow(),
        )
    except Exception as e:
        return CheckResult(
            name=name,
            status=CheckStatus.DOWN,
            error=str(e),
            checked_at=_utcnow(),
        )
