"""Mock OPI terminal simulator (both in-process and real TCP OPI server with Channel 1 DeviceRequest callbacks)."""

from __future__ import annotations

import socket
import socketserver
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .opi import (
    DEFAULT_CHANNEL0_PORT,
     DIAGNOSIS_CODE_MESSAGES,
    OPI_NAMESPACE,
    OPIResponse,
    _find_child,
    _local_name,
    encode_opi_frame,
    format_major_amount,
    next_request_id,
    parse_major_amount_to_cents,
    read_opi_frame,
)


class MockOPIClient:
    """In-process OPI simulator for testing without physical hardware."""

    _stan_counter = 100000
    _lock = threading.Lock()
    _last_tx: dict[str, Any] = {}
    _abort_event = threading.Event()

    def __init__(
        self,
        host: str = "mock",
        port: int = DEFAULT_CHANNEL0_PORT,
        timeout: float = 90.0,
        workstation_id: str = "POS-1",
        language_code: str = "de",
        delay: float = 0.05,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.workstation_id = workstation_id
        self.language_code = language_code
        self.delay = delay

    @classmethod
    def _next_stan(cls) -> str:
        with cls._lock:
            cls._stan_counter += 1
            return f"{cls._stan_counter:06d}"

    def authorize(
        self,
        amount_cents: int,
        currency: str = "CHF",
        reference_number: str | None = None,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        self._abort_event.clear()
        aborted_by_request = False
        if self.delay > 0:
            aborted_by_request = self._abort_event.wait(timeout=self.delay)

        req_id = str(request_id) if request_id else next_request_id()
        stan = self._next_stan()
        statuses = [
            "Bitte Karte einstecken oder vorhalten",
            "Autorisierung läuft...",
        ]
        if on_status:
            for s in statuses:
                on_status(s)

        # 99.95 CHF (9995 cents) or an explicit AbortRequest simulates abort on terminal (Diagnosis 523)
        if amount_cents == 9995 or aborted_by_request or self._abort_event.is_set():
            resp = OPIResponse(
                success=False,
                status="aborted",
                overall_result="Aborted",
                result_code=523,
                result_message=f"Transaktion abgebrochen: {DIAGNOSIS_CODE_MESSAGES[523]} (Code 523)",
                amount_cents=amount_cents,
                currency=currency,
                workstation_id=self.workstation_id,
                request_id=req_id,
                request_type="CardPayment",
                intermediate_statuses=statuses,
                terminal_info={"terminal_id": "12345678", "stan": stan, "mock": True},
                diagnosis={"Module": "EP2", "HostDeclineReason": "0", "TerminalDeclineReason": "523"},
            )
            with self._lock:
                self._last_tx = {
                    "workstation_id": self.workstation_id,
                    "request_id": req_id,
                    "request_type": "CardPayment",
                    "overall_result": "Aborted",
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "receipt_lines": [],
                }
            return resp

        # 99.90 CHF (9990 cents) simulates host decline (Diagnosis 51: Limit exceeded)
        if amount_cents == 9990:
            resp = OPIResponse(
                success=False,
                status="declined",
                overall_result="Failure",
                result_code=51,
                result_message=f"Transaktion fehlgeschlagen oder abgelehnt: {DIAGNOSIS_CODE_MESSAGES[51]} (Code 51)",
                amount_cents=amount_cents,
                currency=currency,
                workstation_id=self.workstation_id,
                request_id=req_id,
                request_type="CardPayment",
                intermediate_statuses=statuses,
                terminal_info={"terminal_id": "12345678", "stan": stan, "mock": True},
                diagnosis={"Module": "EP2", "HostDeclineReason": "51", "TerminalDeclineReason": "0"},
            )
            with self._lock:
                self._last_tx = {
                    "workstation_id": self.workstation_id,
                    "request_id": req_id,
                    "request_type": "CardPayment",
                    "overall_result": "Failure",
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "receipt_lines": [],
                }
            return resp

        amount_str = format_major_amount(amount_cents)
        customer_lines = [
            "KUNDENBELEG (OPI MOCK)",
            "Terminal-ID: 12345678",
            f"STAN:        {stan}",
            f"Betrag:      {currency} {amount_str}",
            "Zahlung erfolgt",
        ]
        merchant_lines = [
            "HÄNDLERBELEG (OPI MOCK)",
            "Terminal-ID: 12345678",
            f"STAN:        {stan}",
            f"Betrag:      {currency} {amount_str}",
            "Zahlung erfolgt",
        ]
        all_lines = merchant_lines + [""] + customer_lines
        with self._lock:
            self._last_tx = {
                "workstation_id": self.workstation_id,
                "request_id": req_id,
                "request_type": "CardPayment",
                "overall_result": "Success",
                "amount_cents": amount_cents,
                "currency": currency,
                "receipt_lines": all_lines,
            }
        return OPIResponse(
            success=True,
            status="approved",
            overall_result="Success",
            result_code=0,
            result_message="Zahlung erfolgreich",
            amount_cents=amount_cents,
            currency=currency,
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="CardPayment",
            receipt_lines=all_lines,
            merchant_receipt_lines=merchant_lines,
            customer_receipt_lines=customer_lines,
            intermediate_statuses=statuses,
            terminal_info={
                "terminal_id": "12345678",
                "stan": stan,
                "approval_code": "123456",
                "trx_reference_number": f"384{stan}",
                "card_circuit": "ECMC",
                "masked_card_number": "xxxxxxxxxxxx1234",
                "mock": True,
            },
            diagnosis={"Module": "EP2", "HostDeclineReason": "0", "TerminalDeclineReason": "0"},
        )

    def refund(
        self,
        amount_cents: int,
        currency: str = "CHF",
        reference_number: str | None = None,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        req_id = str(request_id) if request_id else next_request_id()
        stan = self._next_stan()
        amount_str = format_major_amount(amount_cents)
        lines = [
            "GUTSCHRIFT / REFUND (OPI MOCK)",
            f"STAN:   {stan}",
            f"Betrag: {currency} {amount_str}",
        ]
        return OPIResponse(
            success=True,
            status="approved",
            overall_result="Success",
            result_code=0,
            result_message="Rückerstattung erfolgreich",
            amount_cents=amount_cents,
            currency=currency,
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="PaymentRefund",
            receipt_lines=lines,
            terminal_info={"terminal_id": "12345678", "stan": stan, "mock": True},
        )

    def reversal(
        self,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        req_id = str(request_id) if request_id else next_request_id()
        stan = self._next_stan()
        return OPIResponse(
            success=True,
            status="approved",
            overall_result="Success",
            result_code=0,
            result_message="Storno erfolgreich",
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="PaymentReversal",
            receipt_lines=["STORNO ERFOLGREICH (OPI MOCK)"],
            terminal_info={"terminal_id": "12345678", "stan": stan, "mock": True},
        )

    def reprint(
        self,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        req_id = str(request_id) if request_id else next_request_id()
        with self._lock:
            lines = list(self._last_tx.get("receipt_lines") or ["BELEGKOPIE (OPI MOCK)"])
        return OPIResponse(
            success=True,
            status="ok",
            overall_result="Success",
            result_code=0,
            result_message="Transaktion erfolgreich",
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="TicketReprint",
            receipt_lines=lines,
            terminal_info={"terminal_id": "12345678", "mock": True},
        )

    def recover_last_transaction(
        self,
        expected_original_request_id: str | None = None,
        expected_original_request_type: str = "CardPayment",
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        req_id = str(request_id) if request_id else next_request_id()
        with self._lock:
            last = dict(self._last_tx)
        if not last:
            return OPIResponse(
                success=False,
                status="unknown",
                overall_result="Failure",
                result_code=-1,
                result_message="Keine vorherige Transaktion zur Wiederherstellung vorhanden",
                workstation_id=self.workstation_id,
                request_id=req_id,
                request_type="RepeatLastMessage",
            )
        orig_header = {
            "WorkstationID": last["workstation_id"],
            "RequestID": last["request_id"],
            "RequestType": last["request_type"],
            "OverallResult": last["overall_result"],
        }
        approved = last["overall_result"] == "Success"
        return OPIResponse(
            success=approved,
            status="approved" if approved else "declined",
            overall_result=last["overall_result"],
            result_code=0 if approved else -1,
            result_message=(
                "Zahlung erfolgreich (Wiederhergestellt via RepeatLastMessage)"
                if approved
                else "Transaktion fehlgeschlagen (Wiederhergestellt via RepeatLastMessage)"
            ),
            amount_cents=last.get("amount_cents", 0),
            currency=last.get("currency", "CHF"),
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="RepeatLastMessage",
            original_header=orig_header,
            terminal_info={"terminal_id": "12345678", "original_header": orig_header, "mock": True},
        )

    def status(self) -> OPIResponse:
        return self.service_operation("GetStatus")

    def register(self, **kwargs: Any) -> OPIResponse:
        return self.service_operation("Login")

    def login(self) -> OPIResponse:
        return self.service_operation("Login")

    def close_day(self) -> OPIResponse:
        return self.service_operation("CloseDay")

    def service_operation(
        self,
        request_type: str,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        if self.delay > 0:
            time.sleep(self.delay)
        req_id = str(request_id) if request_id else next_request_id()
        return OPIResponse(
            success=True,
            status="ok",
            overall_result="Success",
            result_code=0,
            result_message=f"{request_type} erfolgreich (Mock)",
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type=request_type,
            terminal_info={"terminal_id": "12345678", "mock": True},
        )

    def abort(self, request_id: str | None = None) -> OPIResponse:
        self._abort_event.set()
        req_id = str(request_id) if request_id else next_request_id()
        return OPIResponse(
            success=True,
            status="ok",
            overall_result="Success",
            result_code=0,
            result_message="Abbruchbefehl vom Terminal bestätigt (Mock)",
            workstation_id=self.workstation_id,
            request_id=req_id,
            request_type="AbortRequest",
            terminal_info={"terminal_id": "12345678", "mock": True},
        )


# Backward-compatible alias
MockZVTClient = MockOPIClient


class _OPITerminalTCPHandler(socketserver.BaseRequestHandler):
    """TCP handler speaking OPI XML protocol (Channel 0 + Channel 1 DeviceRequest callbacks)."""

    def _send_callback(
        self,
        callback_host: str,
        callback_port: int,
        workstation_id: str,
        callback_req_id: str,
        sequence_id: str,
        out_target: str,
        lines: list[str],
        ticket_type: str | None = None,
    ) -> None:
        attrs = {
            "WorkstationID": workstation_id,
            "RequestID": callback_req_id,
            "RequestType": "Output",
            "SequenceID": sequence_id,
            "xmlns": OPI_NAMESPACE,
        }
        root = ET.Element("DeviceRequest", attrs)
        out_attrs = {"OutDeviceTarget": out_target}
        if ticket_type:
            out_attrs["TicketType"] = ticket_type
        out_el = ET.SubElement(root, "Output", out_attrs)
        for line in lines:
            tl = ET.SubElement(out_el, "TextLine")
            tl.text = line

        xml_str = f'<?xml version="1.0" encoding="utf-8"?>\n{ET.tostring(root, encoding="unicode")}'
        with socket.create_connection((callback_host, callback_port), timeout=3.0) as cb_sock:
            cb_sock.settimeout(3.0)
            cb_sock.sendall(encode_opi_frame(xml_str))
            _ = read_opi_frame(cb_sock)

    def handle(self) -> None:
        server: MockOPIServer = self.server.owner  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        sock.settimeout(5.0)
        try:
            raw_xml = read_opi_frame(sock)
            root = ET.fromstring(raw_xml)
        except Exception:
            return

        root_tag = _local_name(root.tag)
        workstation_id = root.attrib.get("WorkstationID", "POS-1")
        request_id = root.attrib.get("RequestID", "1")
        request_type = root.attrib.get("RequestType", "")

        if root_tag == "ServiceRequest":
            resp_xml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                f'<ServiceResponse WorkstationID="{workstation_id}" '
                f'RequestID="{request_id}" '
                f'RequestType="{request_type}" '
                f'OverallResult="Success" '
                f'xmlns="{OPI_NAMESPACE}">'
                '<Terminal TerminalID="12345678" STAN="100001" />'
                "</ServiceResponse>"
            )
            sock.sendall(encode_opi_frame(resp_xml))
            return

        if root_tag == "CardServiceRequest":
            if request_type == "AbortRequest":
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<CardServiceResponse WorkstationID="{workstation_id}" '
                    f'RequestID="{request_id}" '
                    f'RequestType="AbortRequest" '
                    f'OverallResult="Success" '
                    f'xmlns="{OPI_NAMESPACE}" />'
                )
                sock.sendall(encode_opi_frame(resp_xml))
                return

            if request_type == "RepeatLastMessage":
                last = server.last_transaction or {
                    "WorkstationID": workstation_id,
                    "RequestID": "1000",
                    "RequestType": "CardPayment",
                    "OverallResult": "Success",
                    "amount_str": "25.00",
                    "currency": "CHF",
                }
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<CardServiceResponse WorkstationID="{workstation_id}" '
                    f'RequestID="{request_id}" '
                    f'RequestType="RepeatLastMessage" '
                    f'OverallResult="Success" '
                    f'xmlns="{OPI_NAMESPACE}">'
                    '<Terminal TerminalID="12345678" STAN="123456" />'
                    f'<OriginalHeader WorkstationID="{last["WorkstationID"]}" '
                    f'RequestID="{last["RequestID"]}" '
                    f'RequestType="{last["RequestType"]}" '
                    f'OverallResult="{last["OverallResult"]}" />'
                    f'<Tender LanguageCode="de">'
                    f'<TotalAmount Currency="{last.get("currency", "CHF")}">{last.get("amount_str", "0.00")}</TotalAmount>'
                    "</Tender>"
                    "</CardServiceResponse>"
                )
                sock.sendall(encode_opi_frame(resp_xml))
                return

            if request_type == "TicketReprint":
                if server.callback_port:
                    self._send_callback(
                        callback_host=server.callback_host,
                        callback_port=server.callback_port,
                        workstation_id=workstation_id,
                        callback_req_id=f"{request_id}.1",
                        sequence_id="1",
                        out_target="Printer",
                        lines=["BELEGKOPIE", "Zahlung erfolgt"],
                        ticket_type="CustomerReceipt",
                    )
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<CardServiceResponse WorkstationID="{workstation_id}" '
                    f'RequestID="{request_id}" '
                    f'RequestType="TicketReprint" '
                    f'OverallResult="Success" '
                    f'xmlns="{OPI_NAMESPACE}">'
                    '<Terminal TerminalID="12345678" STAN="123456" />'
                    "</CardServiceResponse>"
                )
                sock.sendall(encode_opi_frame(resp_xml))
                return

            # Extract amount and currency
            amount_cents = 0
            amount_str = "0.00"
            currency = "CHF"
            amt_el = _find_child(root, "TotalAmount")
            if amt_el is not None:
                currency = amt_el.attrib.get("Currency", "CHF")
                if amt_el.text:
                    amount_str = amt_el.text.strip()
                    amount_cents = parse_major_amount_to_cents(amount_str)

            # Send Channel 1 display callback if callback_port is configured
            if server.callback_port:
                self._send_callback(
                    callback_host=server.callback_host,
                    callback_port=server.callback_port,
                    workstation_id=workstation_id,
                    callback_req_id=f"{request_id}.1",
                    sequence_id="1",
                    out_target="CashierDisplay",
                    lines=["Bitte Karte vorhalten"],
                )

            # Check special test amounts:
            # 99.95 CHF -> Aborted (TerminalDeclineReason="523")
            if amount_cents == 9995:
                server.last_transaction = {
                    "WorkstationID": workstation_id,
                    "RequestID": request_id,
                    "RequestType": request_type,
                    "OverallResult": "Aborted",
                    "amount_str": amount_str,
                    "currency": currency,
                }
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<CardServiceResponse WorkstationID="{workstation_id}" '
                    f'RequestID="{request_id}" '
                    f'RequestType="{request_type}" '
                    f'OverallResult="Aborted" '
                    f'xmlns="{OPI_NAMESPACE}">'
                    '<Terminal TerminalID="12345678" STAN="123456" />'
                    '<Diagnosis Module="EP2" HostDeclineReason="0" TerminalDeclineReason="523" />'
                    "</CardServiceResponse>"
                )
                sock.sendall(encode_opi_frame(resp_xml))
                return

            # 99.90 CHF -> Declined (HostDeclineReason="51")
            if amount_cents == 9990:
                server.last_transaction = {
                    "WorkstationID": workstation_id,
                    "RequestID": request_id,
                    "RequestType": request_type,
                    "OverallResult": "Failure",
                    "amount_str": amount_str,
                    "currency": currency,
                }
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<CardServiceResponse WorkstationID="{workstation_id}" '
                    f'RequestID="{request_id}" '
                    f'RequestType="{request_type}" '
                    f'OverallResult="Failure" '
                    f'xmlns="{OPI_NAMESPACE}">'
                    '<Terminal TerminalID="12345678" STAN="123456" />'
                    '<Diagnosis Module="EP2" HostDeclineReason="51" TerminalDeclineReason="0" />'
                    "</CardServiceResponse>"
                )
                sock.sendall(encode_opi_frame(resp_xml))
                return

            # Send receipt callback on Channel 1 before final CardServiceResponse
            if server.callback_port:
                self._send_callback(
                    callback_host=server.callback_host,
                    callback_port=server.callback_port,
                    workstation_id=workstation_id,
                    callback_req_id=f"{request_id}.2",
                    sequence_id="2",
                    out_target="Printer",
                    lines=[
                        "KUNDENBELEG",
                        f"BETRAG {currency} {amount_str}",
                        "Zahlung erfolgt",
                    ],
                    ticket_type="CustomerReceipt",
                )

            stan = MockOPIClient._next_stan()
            server.last_transaction = {
                "WorkstationID": workstation_id,
                "RequestID": request_id,
                "RequestType": request_type,
                "OverallResult": "Success",
                "amount_str": amount_str,
                "currency": currency,
            }
            resp_xml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                f'<CardServiceResponse WorkstationID="{workstation_id}" '
                f'RequestID="{request_id}" '
                f'RequestType="{request_type}" '
                f'OverallResult="Success" '
                f'xmlns="{OPI_NAMESPACE}">'
                f'<Terminal TerminalID="12345678" STAN="{stan}" />'
                '<Tender LanguageCode="de">'
                f'<TotalAmount Currency="{currency}">{amount_str}</TotalAmount>'
                f'<Authorisation ApprovalCode="123456" TrxReferenceNumber="384{stan}" '
                'CardCircuit="ECMC" MaskedCardNumber="xxxxxxxxxxxx1234" />'
                "</Tender>"
                '<Diagnosis Module="EP2" HostDeclineReason="0" TerminalDeclineReason="0" />'
                "</CardServiceResponse>"
            )
            sock.sendall(encode_opi_frame(resp_xml))


class _ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MockOPIServer:
    """Standalone background TCP server speaking OPI XML protocol."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        callback_host: str = "127.0.0.1",
        callback_port: int | None = None,
    ) -> None:
        self.callback_host = callback_host
        self.callback_port = callback_port
        self.last_transaction: dict[str, Any] | None = None
        self._server = _ReusableTCPServer((host, port), _OPITerminalTCPHandler)
        self._server.owner = self  # type: ignore[attr-defined]
        self.host, self.port = self._server.server_address
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "MockOPIServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


# Backward-compatible alias
MockZVTServer = MockOPIServer
