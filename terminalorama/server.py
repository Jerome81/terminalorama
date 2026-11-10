"""HTTP/JSON server bridging POS requests to OPI (Open Payment Interface) payment terminals."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .mock_terminal import MockOPIClient
from .opi import (
    DEFAULT_CHANNEL0_PORT,
    DEFAULT_CHANNEL1_PORT,
    OPICallbackServer,
    OPIClient,
    OPIConnectionError,
    OPIError,
    OPITimeoutError,
    next_request_id,
)

logger = logging.getLogger(__name__)


class TerminalRegistry:
    """Manages OPI terminal address resolution, Channel 1 callback listener, and per-terminal concurrency locks."""

    def __init__(
        self,
        default_mock: bool = False,
        config_path: str | None = None,
        workstation_id: str = "POS-1",
        callback_server: OPICallbackServer | None = None,
    ) -> None:
        self.default_mock = default_mock
        self.workstation_id = workstation_id
        self.callback_server = callback_server
        self.terminals: dict[str, dict[str, Any]] = {}
        self._locks: dict[tuple[str, int], threading.Lock] = {}
        self._locks_guard = threading.Lock()

        if config_path and Path(config_path).is_file():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.terminals = data.get("terminals", {})
                if "mock" in data and not default_mock:
                    self.default_mock = bool(data["mock"])
                if "workstation_id" in data:
                    self.workstation_id = str(data["workstation_id"])

    def resolve_target(self, payload: dict[str, Any]) -> tuple[str, int, bool]:
        """
        Resolve (ip, port, is_mock) from request JSON payload or configured terminals.
        Default OPI Channel 0 port is 4100.
        """
        terminal_id = str(payload.get("terminal_id") or "").strip()
        cfg = self.terminals.get(terminal_id, {}) if terminal_id else {}

        ip = str(
            payload.get("ip")
            or cfg.get("ip")
            or os.environ.get("OPI_TERMINAL_IP")
            or os.environ.get("ZVT_TERMINAL_IP")
            or ""
        ).strip()
        port_raw = (
            payload.get("port")
            or cfg.get("port")
            or os.environ.get("OPI_TERMINAL_PORT")
            or DEFAULT_CHANNEL0_PORT
        )
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Ungültiger Port: {port_raw}") from exc

        # Map legacy ZVT default port 20007 to OPI Channel 0 default port 4100
        if port == 20007:
            port = DEFAULT_CHANNEL0_PORT

        is_mock = bool(
            payload.get("mock")
            or cfg.get("mock")
            or self.default_mock
            or ip.lower() in ("mock", "simulator", "test")
        )

        if not ip and is_mock:
            ip = "mock"

        if not ip:
            raise ValueError("Keine Terminal-IP angegeben ('ip' oder 'terminal_id' fehlt)")

        return ip, port, is_mock

    def get_lock(self, ip: str, port: int) -> threading.Lock:
        key = (ip, port)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def create_client(
        self,
        ip: str,
        port: int,
        is_mock: bool,
        timeout: float = 90.0,
        workstation_id: str | None = None,
        language_code: str = "de",
    ) -> OPIClient | MockOPIClient:
        ws_id = workstation_id or self.workstation_id
        if is_mock:
            return MockOPIClient(
                host=ip,
                port=port,
                timeout=timeout,
                workstation_id=ws_id,
                language_code=language_code,
            )
        return OPIClient(
            host=ip,
            port=port,
            timeout=timeout,
            workstation_id=ws_id,
            language_code=language_code,
            callback_server=self.callback_server,
        )


def parse_amount_cents(payload: dict[str, Any]) -> int:
    """Extract amount in cents/Rappen from either `amount_cents` or decimal `amount`."""
    if "amount_cents" in payload and payload["amount_cents"] is not None:
        try:
            cents = int(payload["amount_cents"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Ungültiger Wert für 'amount_cents'") from exc
    elif "amount" in payload and payload["amount"] is not None:
        try:
            dec = Decimal(str(payload["amount"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cents = int(dec * 100)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Ungültiger Wert für 'amount'") from exc
    else:
        raise ValueError("Betrag fehlt ('amount' oder 'amount_cents' erforderlich)")

    if cents <= 0:
        raise ValueError("Betrag muss grösser als 0 sein")
    return cents


class TerminaloramaHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        registry: TerminalRegistry | None = None,
    ) -> None:
        self.registry = registry or TerminalRegistry()
        super().__init__(server_address, TerminaloramaRequestHandler)


class TerminaloramaRequestHandler(BaseHTTPRequestHandler):
    server: TerminaloramaHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ungültiges JSON im Request-Body") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON-Body muss ein Objekt sein")
        return data

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(200, {"status": "ok"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            cb_port = self.server.registry.callback_server.port if self.server.registry.callback_server else None
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "terminalorama",
                    "protocol": "OPI",
                    "mock_mode": self.server.registry.default_mock,
                    "callback_port": cb_port,
                },
            )
        else:
            self._send_json(404, {"success": False, "error": f"Unbekannter Pfad: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"success": False, "status": "error", "result_message": str(exc)})
            return
        logger.info("Received request for path %s with payload %s", path, payload)
        print(f"Received request for path {path} with payload {payload}")
        try:
            if path == "/pay":
                self._handle_pay(payload)
            elif path == "/refund":
                self._handle_refund(payload)
            elif path == "/reversal":
                self._handle_reversal(payload)
            elif path == "/reprint":
                self._handle_reprint(payload)
            elif path == "/recover":
                self._handle_recover(payload)
            elif path == "/status":
                self._handle_status(payload)
            elif path in ("/register", "/login"):
                self._handle_login(payload)
            elif path == "/close_day":
                self._handle_close_day(payload)
            elif path == "/abort":
                self._handle_abort(payload)
            else:
                self._send_json(404, {"success": False, "status": "error", "result_message": f"Unbekannter Pfad: {path}"})
        except ValueError as exc:
            self._send_json(400, {"success": False, "status": "error", "result_message": str(exc)})
        except OPITimeoutError as exc:
            self._send_json(504, {"success": False, "status": "timeout", "result_message": str(exc)})
        except OPIConnectionError as exc:
            self._send_json(502, {"success": False, "status": "connection_error", "result_message": str(exc)})
        except OPIError as exc:
            self._send_json(500, {"success": False, "status": "error", "result_message": str(exc)})
        except Exception as exc:
            logger.exception("Unexpected error handling %s", path)
            self._send_json(500, {"success": False, "status": "error", "result_message": f"Interner Serverfehler: {exc}"})

    def _acquire_terminal_lock(self, ip: str, port: int) -> threading.Lock | None:
        lock = self.server.registry.get_lock(ip, port)
        if not lock.acquire(timeout=2.0):
            self._send_json(
                409,
                {
                    "success": False,
                    "status": "busy",
                    "result_message": f"Terminal {ip}:{port} ist bereits mit einer Transaktion beschäftigt",
                },
            )
            return None
        return lock

    def _handle_pay(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        amount_cents = parse_amount_cents(payload)
        currency = str(payload.get("currency") or "CHF").upper()
        timeout = float(payload.get("timeout") or 90.0)
        reference_number = payload.get("reference_number")
        workstation_id = payload.get("workstation_id")

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        req_id = str(payload.get("request_id") or next_request_id())
        try:
            client = self.server.registry.create_client(
                ip, port, is_mock, timeout=timeout, workstation_id=workstation_id
            )
            try:
                resp = client.authorize(
                    amount_cents=amount_cents,
                    currency=currency,
                    reference_number=reference_number,
                    request_id=req_id,
                )
            except OPITimeoutError:
                # Per OPI validation docs: if final response was lost after sending CardPayment,
                # attempt RepeatLastMessage recovery before returning timeout.
                logger.warning(
                    "Timeout waiting for CardPayment response on %s:%d (RequestID=%s); attempting RepeatLastMessage recovery",
                    ip,
                    port,
                    req_id,
                )
                try:
                    rec_resp = client.recover_last_transaction(
                        expected_original_request_id=req_id,
                        expected_original_request_type="CardPayment",
                    )
                    if rec_resp.status != "unknown":
                        http_code = 200 if rec_resp.success else 422
                        self._send_json(http_code, rec_resp.to_dict())
                        return
                except Exception as rec_exc:
                    logger.warning("RepeatLastMessage recovery after timeout failed: %s", rec_exc)
                raise

            # If terminal reports Loggedout, perform Login and retry once with a new RequestID
            if resp.overall_result == "Loggedout":
                logger.info("Terminal %s:%d reported Loggedout; executing Login first", ip, port)
                login_resp = client.login()
                if login_resp.success:
                    retry_req_id = next_request_id()
                    resp = client.authorize(
                        amount_cents=amount_cents,
                        currency=currency,
                        reference_number=reference_number,
                        request_id=retry_req_id,
                    )

            http_code = 200 if resp.success else 422
            self._send_json(http_code, resp.to_dict())
        finally:
            lock.release()

    def _handle_refund(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        amount_cents = parse_amount_cents(payload)
        currency = str(payload.get("currency") or "CHF").upper()
        timeout = float(payload.get("timeout") or 90.0)
        reference_number = payload.get("reference_number")

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.refund(
                amount_cents=amount_cents,
                currency=currency,
                reference_number=reference_number,
            )
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_reversal(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 90.0)

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.reversal()
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_reprint(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 30.0)

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.reprint()
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_recover(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 30.0)
        expected_id = payload.get("original_request_id")
        expected_type = str(payload.get("original_request_type") or "CardPayment")

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.recover_last_transaction(
                expected_original_request_id=str(expected_id) if expected_id else None,
                expected_original_request_type=expected_type,
            )
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_status(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 15.0)

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.status()
            self._send_json(200 if resp.success else 502, resp.to_dict())
        finally:
            lock.release()

    def _handle_login(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 30.0)

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.login()
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_close_day(self, payload: dict[str, Any]) -> None:
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 60.0)

        lock = self._acquire_terminal_lock(ip, port)
        if lock is None:
            return

        try:
            client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
            resp = client.close_day()
            self._send_json(200 if resp.success else 422, resp.to_dict())
        finally:
            lock.release()

    def _handle_abort(self, payload: dict[str, Any]) -> None:
        # Per Nexi OPI docs, AbortRequest is sent on a separate TCP connection while
        # the payment connection is still active, so it intentionally bypasses the per-terminal lock.
        ip, port, is_mock = self.server.registry.resolve_target(payload)
        timeout = float(payload.get("timeout") or 10.0)
        client = self.server.registry.create_client(ip, port, is_mock, timeout=timeout)
        resp = client.abort()
        self._send_json(200 if resp.success else 422, resp.to_dict())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Terminalorama - HTTP/JSON to OPI payment terminal bridge")
    parser.add_argument(
        "--host",
        default=os.environ.get("TERMINALORAMA_HOST", "127.0.0.1"),
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TERMINALORAMA_PORT", "8080")),
        help="HTTP bind port (default: 8080)",
    )
    parser.add_argument(
        "--callback-host",
        default=os.environ.get("TERMINALORAMA_CALLBACK_HOST", "0.0.0.0"),
        help="OPI Channel 1 DeviceRequest callback bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--callback-port",
        type=int,
        default=int(os.environ.get("TERMINALORAMA_CALLBACK_PORT", str(DEFAULT_CHANNEL1_PORT))),
        help=f"OPI Channel 1 DeviceRequest callback TCP port (default: {DEFAULT_CHANNEL1_PORT})",
    )
    parser.add_argument(
        "--workstation-id",
        default=os.environ.get("TERMINALORAMA_WORKSTATION_ID", "POS-1"),
        help="OPI WorkstationID sent in XML requests (default: POS-1)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=os.environ.get("TERMINALORAMA_MOCK", "").lower() in ("1", "true", "yes"),
        help="Enable mock terminal mode by default",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("TERMINALORAMA_CONFIG"),
        help="Path to optional JSON configuration file",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    callback_server = OPICallbackServer(host=args.callback_host, port=args.callback_port)
    callback_server.start()

    registry = TerminalRegistry(
        default_mock=args.mock,
        config_path=args.config,
        workstation_id=args.workstation_id,
        callback_server=callback_server,
    )
    server = TerminaloramaHTTPServer((args.host, args.port), registry=registry)
    logger.info(
        "Terminalorama (OPI) listening on http://%s:%d (Channel 1 callback=%s:%d, default_mock=%s)",
        args.host,
        args.port,
        callback_server.host,
        callback_server.port,
        registry.default_mock,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Terminalorama...")
    finally:
        server.server_close()
        callback_server.stop()


if __name__ == "__main__":
    main()
