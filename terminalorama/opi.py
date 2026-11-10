"""OPI (Open Payment Interface) protocol framing, XML builder/parser, Channel 0 client, and Channel 1 callback listener."""

from __future__ import annotations

import datetime
import logging
import os
import socket
import socketserver
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

OPI_NAMESPACE = "http://www.nrf-arts.org/IXRetail/namespace"
ET.register_namespace("", OPI_NAMESPACE)

DEFAULT_CHANNEL0_PORT = 4100  # POS -> Terminal (CardServiceRequest / ServiceRequest)
DEFAULT_CHANNEL1_PORT = 4102  # Terminal -> POS (DeviceRequest callbacks)
MAX_FRAME_SIZE = 1_048_576    # 1 MB safety limit for incoming XML frames

# OPI OverallResult meanings (German) based on https://docs.nexi.swiss/de/opi-response-codes
OVERALL_RESULT_MESSAGES: dict[str, str] = {
    "Success": "Transaktion erfolgreich",
    "Failure": "Transaktion fehlgeschlagen oder abgelehnt",
    "Aborted": "Transaktion abgebrochen",
    "TimedOut": "Zeitüberschreitung am Terminal",
    "DeviceUnavailable": "Terminal nicht verfügbar oder beschäftigt",
    "Loggedout": "Terminal ist abgemeldet (Login erforderlich)",
    "PrintLastTicket": "Letzter Beleg muss zuerst gedruckt werden (TicketReprint erforderlich)",
    "FormatError": "OPI-Formatfehler in der Anfrage",
    "ParsingError": "OPI-XML-Parsing-Fehler",
    "ValidationError": "OPI-XML-Validierungsfehler",
    "MissingMandatoryData": "Pflichtfelder in der OPI-Anfrage fehlen",
}

# Numeric Diagnosis codes (HostDeclineReason / TerminalDeclineReason) based on https://docs.nexi.swiss/de/opi-response-codes
DIAGNOSIS_CODE_MESSAGES: dict[int, str] = {
    0: "Vorgang ohne Fehler abgeschlossen",
    2: "Zahlung abgelehnt (Telefonische Autorisierung ggf. erforderlich)",
    3: "Ungültige Vertragspartner-ID (Card Acceptor ID)",
    4: "Karte nicht akzeptiert oder Autorisierung abgelehnt",
    5: "Autorisierung abgelehnt",
    12: "Ungültige Transaktion (z. B. nicht unterstützte Währung)",
    13: "Verfügbarer Transaktionsbetrag überschritten",
    14: "Ungültige Karte",
    21: "Originaltransaktion nicht gefunden",
    30: "Formatfehler",
    33: "Karte abgelaufen",
    40: "Angeforderte Funktion wird nicht unterstützt",
    50: "Doppelte Autorisierung",
    51: "Kartenlimit überschritten / Deckung ungenügend",
    54: "Karte abgelaufen",
    55: "Falsche PIN eingegeben",
    57: "Andere Karte als bei der Originaltransaktion verwendet",
    62: "Karte gesperrt",
    64: "Betrag weicht von der Originaltransaktion ab",
    68: "Keine Antwort vom Autorisierungssystem",
    75: "Maximale Anzahl PIN-Versuche überschritten",
    91: "Kartenherausgeber oder Netzwerk nicht erreichbar",
    418: "Ungültige Währung",
    501: "Nicht unterstütztes Protokoll oder XML-Version",
    502: "Tagesabschluss abgebrochen (offene Transaktionen vorhanden)",
    504: "Zeitüberschreitung bei Sprachauswahl oder PIN-Eingabe",
    505: "Vorgang abgebrochen (fehlende Initialisierung/TID oder Benutzer-Timeout)",
    507: "Drucker nicht bereit",
    508: "Terminal bereits in Verwendung (andere Operation aktiv)",
    509: "Server inaktiv oder Terminal nicht aktiviert",
    510: "Server aktiv (Ping-Antwort)",
    512: "Keine Umsätze seit dem letzten Tagesabschluss",
    513: "Kassenanzeige nicht bereit oder antwortet nicht",
    515: "Tagesabschluss durch Offline-Transaktionen blockiert",
    516: "Offline-Limit überschritten",
    517: "Ungültig formatierte Kassenanfrage",
    518: "PIN-Eingabe fehlgeschlagen",
    519: "PIN-Pad nicht verfügbar oder nicht bereit",
    520: "Kartenleser- oder Terminalkonfiguration fehlt/ungültig",
    523: "Abbruch-Taste am Terminal gedrückt",
    524: "Zeitüberschreitung beim Karteneinschub",
    527: "PIN-Pad nicht bereit",
    528: "Terminalkonfiguration fehlt oder ist ungültig",
    529: "Interner Formatfehler",
    533: "Betrag fehlt",
    534: "Betrag ausserhalb des gültigen Bereichs",
    535: "Ungültiges Betragsformat",
    538: "Keine Storno-Referenz gefunden",
    539: "Ungültige Kassen- oder Filialkennung",
    540: "Storno wegen Trinkgeld-Update nicht möglich",
    541: "Startvorgang fehlgeschlagen",
    542: "Storno-Karte stimmt nicht mit Originalkarte überein",
    544: "Allgemeiner Transaktionsfehler",
    545: "Letzter Druck fehlgeschlagen (Beleg-Nachdruck erforderlich)",
    546: "Neustart des PIN-Pads nach Konfiguration erforderlich",
    552: "Zeitüberschreitung bei Antwort vom PIN-Pad",
    553: "Kommunikationsproblem zwischen Server und PIN-Pad",
    555: "Notfall-Offline-Datensätze wurden noch nicht übertragen",
    565: "Terminal erhielt keine Antwort von der Kasse (Callback fehlgeschlagen)",
    568: "Fehlende oder ungültige Antwort vom Host",
    569: "Keine TCP-Verbindung zum Autorisierungs-Host",
    570: "Pflichtfelder in der Antwort stimmen nicht überein",
    571: "Unerwartete Antwort vom Host",
    572: "Fehler während der Online-Kommunikation",
    573: "Eingehende Host-Nachricht hat Schema-Validierung nicht bestanden",
    574: "Eingehender Host-Nachricht fehlt ein Pflichtelement",
    578: "Fehlende oder ungültige Antwort vom acCEPT-Server",
    579: "Keine Verbindung zum acCEPT-Server",
    581: "acCEPT-Datenbankfehler",
    681: "Automatisches Storno konnte nicht durchgeführt werden",
    695: "Warenausgabe meldete fehlgeschlagene Ausgabe",
    696: "Keine Antwort von der Warenausgabe",
    999: "Interner Verarbeitungsfehler",
    1101: "Keine passende EMV-Applikation für diese Karte konfiguriert",
    1102: "Karteninhaber hat die Applikationsauswahl abgebrochen",
    1103: "Online-Autorisierung erforderlich, konnte aber nicht durchgeführt werden",
    1104: "Karte oder Terminal hat die Autorisierung abgelehnt",
    1106: "Kunde hat die Transaktion abgebrochen",
}


class OPIError(Exception):
    """Base exception for OPI protocol or transport errors."""


class OPITimeoutError(OPIError):
    """Raised when the OPI terminal does not respond within the timeout.
    Note: If a financial request was already sent, the outcome may be unknown
    and should be checked via RepeatLastMessage."""


class OPIConnectionError(OPIError):
    """Raised when TCP connection to the OPI terminal fails."""


class RequestIDGenerator:
    """Thread-safe, monotonically increasing RequestID generator avoiding reuse across restarts."""

    def __init__(self, state_file: str | None = None) -> None:
        self._lock = threading.Lock()
        self._state_file = Path(state_file) if state_file else None
        # Initialize from epoch milliseconds mod 1_000_000_000 so restarts never collide
        self._counter = int(time.time() * 1000) % 1_000_000_000
        if self._state_file and self._state_file.is_file():
            try:
                saved = int(self._state_file.read_text(encoding="utf-8").strip())
                if saved > self._counter:
                    self._counter = saved
            except (ValueError, OSError):
                pass

    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            val = self._counter
            if self._state_file:
                try:
                    self._state_file.write_text(str(val), encoding="utf-8")
                except OSError:
                    pass
            return str(val)


_default_id_generator = RequestIDGenerator()


def next_request_id() -> str:
    """Return a fresh, unique OPI RequestID string."""
    return _default_id_generator.next_id()


# ---------------------------------------------------------------------------
# TCP 4-byte Big-Endian Length Framing
# ---------------------------------------------------------------------------

def encode_opi_frame(xml_text: str) -> bytes:
    """
    Encode an XML string into an OPI TCP frame:
    [4-byte big-endian unsigned byte length][UTF-8 XML bytes]
    """
    payload = xml_text.encode("utf-8")
    header = struct.pack("!I", len(payload))
    return header + payload


def parse_opi_frame(raw: bytes) -> tuple[str, bytes]:
    """
    Parse a single OPI frame from the start of `raw`.
    Returns (xml_string, remaining_bytes).
    """
    if len(raw) < 4:
        raise ValueError("Unvollständiger OPI-Frame-Header (weniger als 4 Bytes)")
    (length,) = struct.unpack("!I", raw[:4])
    if length == 0 or length > MAX_FRAME_SIZE:
        raise ValueError(f"Ungültige OPI-Frame-Länge: {length} Bytes")
    if len(raw) < 4 + length:
        raise ValueError(f"Unvollständiger OPI-Frame-Payload (erwartet: {length}, erhalten: {len(raw) - 4})")
    xml_bytes = raw[4 : 4 + length]
    remaining = raw[4 + length :]
    return xml_bytes.decode("utf-8"), remaining


def _recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise OPIConnectionError("TCP-Verbindung vom Terminal geschlossen")
        buf.extend(chunk)
    return bytes(buf)


def read_opi_frame(sock: socket.socket) -> str:
    """Read a complete 4-byte length-prefixed UTF-8 XML message from `sock`."""
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    if length == 0 or length > MAX_FRAME_SIZE:
        raise OPIError(f"Ungültige OPI-Nachrichtenlänge vom Terminal: {length} Bytes")
    payload = _recv_exact(sock, length)
    return payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# XML Construction (CardServiceRequest, ServiceRequest, DeviceResponse)
# ---------------------------------------------------------------------------

def _iso_timestamp() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def format_major_amount(amount_cents: int) -> str:
    """Convert integer minor units (cents/Rappen) to major currency string '25.00'."""
    dec = (Decimal(amount_cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{dec:.2f}"


def parse_major_amount_to_cents(amount_str: str) -> int:
    """Convert major currency string '25.00' to integer minor units (2500)."""
    dec = Decimal(amount_str.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(dec * 100)


def build_card_service_request(
    request_type: str,
    workstation_id: str = "POS-1",
    request_id: str | None = None,
    amount_cents: int | None = None,
    currency: str = "CHF",
    reference_number: str | None = None,
    language_code: str = "de",
    shift_number: str | None = None,
    clerk_id: str | None = None,
    printer_status: str = "Available",
    e_journal_status: str = "Available",
    journal_printer_status: str = "Available",
    request_transaction_info: bool = True,
    trx_reference_number: str | None = None,
    acquirer_identifier: str | None = None,
    force_acceptance: bool = False,
    adjustment: bool = False,
    timestamp: str | None = None,
) -> tuple[str, str]:
    """
    Build an OPI `<CardServiceRequest>` XML string.
    Returns (xml_string, request_id).
    """
    req_id = str(request_id) if request_id is not None else next_request_id()
    attrs: dict[str, str] = {
        "WorkstationID": workstation_id,
        "RequestID": req_id,
        "RequestType": request_type,
    }
    if reference_number:
        attrs["ReferenceNumber"] = str(reference_number)
    attrs["xmlns"] = OPI_NAMESPACE

    root = ET.Element("CardServiceRequest", attrs)

    pos_attrs: dict[str, str] = {"LanguageCode": language_code}
    if request_transaction_info and request_type != "AbortRequest":
        pos_attrs["RequestTransactionInformation"] = "true"
    if trx_reference_number:
        pos_attrs["TrxReferenceNumber"] = str(trx_reference_number)
    if acquirer_identifier:
        pos_attrs["AcquirerIdentifier"] = str(acquirer_identifier)

    pos_data = ET.SubElement(root, "POSdata", pos_attrs)
    ts_el = ET.SubElement(pos_data, "POSTimeStamp")
    ts_el.text = timestamp or _iso_timestamp()

    if shift_number is not None:
        shift_el = ET.SubElement(pos_data, "ShiftNumber")
        shift_el.text = str(shift_number)
    if clerk_id is not None:
        clerk_el = ET.SubElement(pos_data, "ClerkID")
        clerk_el.text = str(clerk_id)

    if request_type != "AbortRequest":
        ps_el = ET.SubElement(pos_data, "PrinterStatus")
        ps_el.text = printer_status
        ej_el = ET.SubElement(pos_data, "E-JournalStatus")
        ej_el.text = e_journal_status
        jp_el = ET.SubElement(pos_data, "JournalPrinterStatus")
        jp_el.text = journal_printer_status

    if force_acceptance:
        fa_el = ET.SubElement(pos_data, "ForceAcceptance")
        fa_el.text = "true"
    if adjustment:
        adj_el = ET.SubElement(pos_data, "Adjustment")
        adj_el.text = "true"

    if amount_cents is not None and request_type in ("CardPayment", "PaymentRefund", "CardReservation"):
        if amount_cents <= 0:
            raise ValueError("amount_cents muss grösser als 0 sein")
        amt_el = ET.SubElement(root, "TotalAmount", {"Currency": currency.upper()})
        amt_el.text = format_major_amount(amount_cents)

    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="utf-8"?>\n{xml_body}', req_id


def build_service_request(
    request_type: str,
    workstation_id: str = "POS-1",
    request_id: str | None = None,
    language_code: str = "de",
    shift_number: str | None = None,
    clerk_id: str | None = None,
    printer_status: str = "Available",
    e_journal_status: str = "Available",
    journal_printer_status: str = "Available",
    timestamp: str | None = None,
) -> tuple[str, str]:
    """
    Build an OPI `<ServiceRequest>` XML string (Login, Logoff, GetStatus, GetInfo, CloseDay, TransmitTrx, ContactTMS, ContactAcq).
    Returns (xml_string, request_id).
    """
    req_id = str(request_id) if request_id is not None else next_request_id()
    attrs: dict[str, str] = {
        "WorkstationID": workstation_id,
        "RequestID": req_id,
        "RequestType": request_type,
        "xmlns": OPI_NAMESPACE,
    }
    root = ET.Element("ServiceRequest", attrs)

    pos_data = ET.SubElement(root, "POSdata", {"LanguageCode": language_code})
    ts_el = ET.SubElement(pos_data, "POSTimeStamp")
    ts_el.text = timestamp or _iso_timestamp()

    if shift_number is not None:
        shift_el = ET.SubElement(pos_data, "ShiftNumber")
        shift_el.text = str(shift_number)
    if clerk_id is not None:
        clerk_el = ET.SubElement(pos_data, "ClerkID")
        clerk_el.text = str(clerk_id)

    ps_el = ET.SubElement(pos_data, "PrinterStatus")
    ps_el.text = printer_status
    ej_el = ET.SubElement(pos_data, "E-JournalStatus")
    ej_el.text = e_journal_status
    jp_el = ET.SubElement(pos_data, "JournalPrinterStatus")
    jp_el.text = journal_printer_status

    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="utf-8"?>\n{xml_body}', req_id


@dataclass
class ParsedDeviceRequest:
    """Parsed representation of an incoming `<DeviceRequest>` callback from the terminal."""

    workstation_id: str
    request_id: str
    request_type: str
    sequence_id: str | None = None
    out_device_target: str | None = None
    ticket_type: str | None = None
    text_lines: list[str] = field(default_factory=list)
    e_journal_xml: str | None = None
    in_device_target: str | None = None
    input_command: str | None = None
    raw_xml: str = ""


def build_device_response(
    device_req: ParsedDeviceRequest,
    overall_result: str = "Success",
    out_result: str = "Success",
    in_result: str = "Success",
    input_boolean: bool | None = None,
    input_string: str | None = None,
    input_number: int | None = None,
) -> str:
    """
    Build a matching `<DeviceResponse>` for a received `<DeviceRequest>`.
    Always copies WorkstationID, RequestID, and RequestType from the callback itself.
    """
    attrs: dict[str, str] = {
        "WorkstationID": device_req.workstation_id,
        "RequestID": device_req.request_id,
        "RequestType": device_req.request_type,
        "OverallResult": overall_result,
        "xmlns": OPI_NAMESPACE,
    }
    root = ET.Element("DeviceResponse", attrs)

    if device_req.out_device_target:
        ET.SubElement(
            root,
            "Output",
            {
                "OutDeviceTarget": device_req.out_device_target,
                "OutResult": out_result,
            },
        )

    if device_req.in_device_target:
        inp_el = ET.SubElement(
            root,
            "Input",
            {
                "InDeviceTarget": device_req.in_device_target,
                "InResult": in_result,
            },
        )
        if input_boolean is not None or input_string is not None or input_number is not None:
            val_el = ET.SubElement(inp_el, "InputValue")
            if input_boolean is not None:
                b_el = ET.SubElement(val_el, "InBoolean")
                b_el.text = "true" if input_boolean else "false"
            elif input_string is not None:
                s_el = ET.SubElement(val_el, "InString")
                s_el.text = str(input_string)
            elif input_number is not None:
                n_el = ET.SubElement(val_el, "InNumber")
                n_el.text = str(input_number)

    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="utf-8"?>\n{xml_body}'


# ---------------------------------------------------------------------------
# XML Parsing
# ---------------------------------------------------------------------------

def _local_name(tag: str) -> str:
    """Strip XML namespace URI from an ElementTree tag `{ns}TagName` -> `TagName`."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local_name(child.tag) == name:
            return child
    return None


def _find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem if _local_name(child.tag) == name]


def parse_device_request(xml_str: str) -> ParsedDeviceRequest:
    """Parse an incoming `<DeviceRequest>` XML string."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        raise OPIError(f"Ungültiges DeviceRequest-XML: {exc}") from exc

    if _local_name(root.tag) != "DeviceRequest":
        raise OPIError(f"Erwartete DeviceRequest, erhielt {_local_name(root.tag)}")

    workstation_id = root.attrib.get("WorkstationID", "")
    request_id = root.attrib.get("RequestID", "")
    request_type = root.attrib.get("RequestType", "Output")
    sequence_id = root.attrib.get("SequenceID")

    out_target = None
    ticket_type = None
    text_lines: list[str] = []
    e_journal_xml = None

    output_el = _find_child(root, "Output")
    if output_el is not None:
        out_target = output_el.attrib.get("OutDeviceTarget")
        ticket_type = output_el.attrib.get("TicketType")
        for tl in _find_children(output_el, "TextLine"):
            text_lines.append(tl.text if tl.text is not None else "")
        ej_el = _find_child(output_el, "E-Journal")
        if ej_el is not None:
            e_journal_xml = ET.tostring(ej_el, encoding="unicode")

    in_target = None
    input_cmd = None
    input_el = _find_child(root, "Input")
    if input_el is not None:
        in_target = input_el.attrib.get("InDeviceTarget")
        cmd_el = _find_child(input_el, "Command")
        if cmd_el is not None and cmd_el.text:
            input_cmd = cmd_el.text.strip()
        else:
            input_cmd = input_el.attrib.get("Command")

    return ParsedDeviceRequest(
        workstation_id=workstation_id,
        request_id=request_id,
        request_type=request_type,
        sequence_id=sequence_id,
        out_device_target=out_target,
        ticket_type=ticket_type,
        text_lines=text_lines,
        e_journal_xml=e_journal_xml,
        in_device_target=in_target,
        input_command=input_cmd,
        raw_xml=xml_str,
    )


@dataclass
class OPIResponse:
    """Structured result of an OPI CardService or Service operation."""

    success: bool
    status: str  # "approved", "declined", "aborted", "timeout", "unknown", "error", "ok"
    overall_result: str = ""
    result_code: int = 0
    result_message: str = ""
    amount_cents: int = 0
    currency: str = "CHF"
    workstation_id: str = ""
    request_id: str = ""
    request_type: str = ""
    receipt_lines: list[str] = field(default_factory=list)
    merchant_receipt_lines: list[str] = field(default_factory=list)
    customer_receipt_lines: list[str] = field(default_factory=list)
    intermediate_statuses: list[str] = field(default_factory=list)
    terminal_info: dict[str, Any] = field(default_factory=dict)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    original_header: dict[str, str] | None = None
    raw_xml: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "overall_result": self.overall_result,
            "result_code": self.result_code,
            "result_message": self.result_message,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "workstation_id": self.workstation_id,
            "request_id": self.request_id,
            "request_type": self.request_type,
            "receipt_lines": self.receipt_lines,
            "merchant_receipt_lines": self.merchant_receipt_lines,
            "customer_receipt_lines": self.customer_receipt_lines,
            "intermediate_statuses": self.intermediate_statuses,
            "terminal_info": self.terminal_info,
            "diagnosis": self.diagnosis,
            "original_header": self.original_header,
        }


def _describe_opi_outcome(
    overall_result: str,
    request_type: str,
    host_decline: int,
    term_decline: int,
) -> tuple[bool, str, int, str]:
    """
    Map OPI `OverallResult` + `Diagnosis` codes into (success, status, result_code, german_message).
    """
    diag_code = term_decline if term_decline != 0 else host_decline

    if overall_result == "Success":
        if request_type in ("CardPayment", "PaymentRefund", "CardReservation", "PaymentReversal", "CancelReservation"):
            status = "approved"
        else:
            status = "ok"
        base_msg = (
            "Zahlung erfolgreich"
            if request_type == "CardPayment"
            else "Rückerstattung erfolgreich"
            if request_type == "PaymentRefund"
            else "Storno erfolgreich"
            if request_type == "PaymentReversal"
            else OVERALL_RESULT_MESSAGES.get(overall_result, "Erfolgreich")
        )
        return True, status, diag_code, base_msg

    if overall_result == "Aborted" or diag_code in (505, 523, 1102, 1106):
        status = "aborted"
    elif overall_result == "TimedOut" or diag_code in (504, 524, 552):
        status = "timeout"
    elif overall_result == "Failure":
        status = "declined"
    else:
        status = "error"

    base_msg = OVERALL_RESULT_MESSAGES.get(overall_result, f"OPI-Ergebnis: {overall_result}")
    if diag_code != 0:
        diag_text = DIAGNOSIS_CODE_MESSAGES.get(diag_code, f"Diagnosecode {diag_code}")
        msg = f"{base_msg}: {diag_text} (Code {diag_code})"
    else:
        msg = base_msg

    return False, status, diag_code if diag_code != 0 else -1, msg


def parse_opi_response(
    xml_str: str,
    default_amount_cents: int = 0,
    default_currency: str = "CHF",
) -> OPIResponse:
    """
    Parse `<CardServiceResponse>` or `<ServiceResponse>` XML into an `OPIResponse`.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        raise OPIError(f"Ungültiges OPI-Response-XML: {exc}") from exc

    root_name = _local_name(root.tag)
    if root_name not in ("CardServiceResponse", "ServiceResponse"):
        raise OPIError(f"Unerwartetes OPI-Antwort-Element: {root_name}")

    workstation_id = root.attrib.get("WorkstationID", "")
    request_id = root.attrib.get("RequestID", "")
    request_type = root.attrib.get("RequestType", "")
    overall_result = root.attrib.get("OverallResult", "Failure")

    terminal_info: dict[str, Any] = {}
    diagnosis: dict[str, Any] = {}
    amount_cents = default_amount_cents
    currency = default_currency

    # <Terminal TerminalID="..." STAN="..." />
    term_el = _find_child(root, "Terminal")
    if term_el is not None:
        if "TerminalID" in term_el.attrib:
            terminal_info["terminal_id"] = term_el.attrib["TerminalID"]
        if "STAN" in term_el.attrib:
            terminal_info["stan"] = term_el.attrib["STAN"]

    # <Tender>
    tender_el = _find_child(root, "Tender")
    if tender_el is not None:
        amt_el = _find_child(tender_el, "TotalAmount")
        if amt_el is not None:
            if "Currency" in amt_el.attrib:
                currency = amt_el.attrib["Currency"]
            if amt_el.text and amt_el.text.strip():
                try:
                    amount_cents = parse_major_amount_to_cents(amt_el.text)
                except Exception:
                    pass
            elif "PaymentAmount" in amt_el.attrib:
                try:
                    amount_cents = parse_major_amount_to_cents(amt_el.attrib["PaymentAmount"])
                except Exception:
                    pass

        auth_el = _find_child(tender_el, "Authorisation")
        if auth_el is None:
            auth_el = _find_child(tender_el, "Authorization")
        if auth_el is not None:
            for attr_key, info_key in (
                ("ApprovalCode", "approval_code"),
                ("TrxReferenceNumber", "trx_reference_number"),
                ("CardCircuit", "card_circuit"),
                ("MaskedCardNumber", "masked_card_number"),
                ("AcquirerBatch", "acquirer_batch"),
                ("TimeStamp", "timestamp"),
                ("ReceiptCopies", "receipt_copies"),
            ):
                if attr_key in auth_el.attrib:
                    terminal_info[info_key] = auth_el.attrib[attr_key]

            for reason_attr in ("HostDeclineReason", "HostdeclineReason"):
                if reason_attr in auth_el.attrib:
                    diagnosis["HostDeclineReason"] = auth_el.attrib[reason_attr]
            if "TerminalDeclineReason" in auth_el.attrib:
                diagnosis["TerminalDeclineReason"] = auth_el.attrib["TerminalDeclineReason"]

    # <Diagnosis Module="EP2" HostDeclineReason="0" TerminalDeclineReason="0" />
    diag_el = _find_child(root, "Diagnosis")
    if diag_el is not None:
        for k, v in diag_el.attrib.items():
            diagnosis[k] = v

    host_decline = 0
    term_decline = 0
    try:
        host_decline = int(diagnosis.get("HostDeclineReason") or diagnosis.get("HostdeclineReason") or 0)
    except ValueError:
        pass
    try:
        term_decline = int(diagnosis.get("TerminalDeclineReason") or 0)
    except ValueError:
        pass

    # <OriginalHeader> (used in RepeatLastMessage recovery responses)
    original_header: dict[str, str] | None = None
    orig_el = _find_child(root, "OriginalHeader")
    if orig_el is not None:
        original_header = dict(orig_el.attrib)
        terminal_info["original_header"] = original_header

    # <Reconciliation> (used in CloseDay service responses)
    reconcil_el = _find_child(root, "Reconciliation")
    if reconcil_el is not None:
        reconcil_info: dict[str, Any] = dict(reconcil_el.attrib)
        totals = []
        for ct in _find_children(reconcil_el, "CircuitTotals"):
            entry = dict(ct.attrib)
            amt_child = _find_child(ct, "TotalAmount")
            if amt_child is not None and amt_child.text:
                entry["amount"] = amt_child.text.strip()
                if "Currency" in amt_child.attrib:
                    entry["currency"] = amt_child.attrib["Currency"]
            elif ct.text and ct.text.strip():
                entry["amount"] = ct.text.strip()
            totals.append(entry)
        if totals:
            reconcil_info["circuit_totals"] = totals
        terminal_info["reconciliation"] = reconcil_info

    success, status, result_code, result_message = _describe_opi_outcome(
        overall_result=overall_result,
        request_type=request_type,
        host_decline=host_decline,
        term_decline=term_decline,
    )

    if diagnosis:
        terminal_info["diagnosis"] = diagnosis

    return OPIResponse(
        success=success,
        status=status,
        overall_result=overall_result,
        result_code=result_code,
        result_message=result_message,
        amount_cents=amount_cents,
        currency=currency,
        workstation_id=workstation_id,
        request_id=request_id,
        request_type=request_type,
        terminal_info=terminal_info,
        diagnosis=diagnosis,
        original_header=original_header,
        raw_xml=xml_str,
    )


# ---------------------------------------------------------------------------
# Channel 1 Callback Collector & Listener (Terminal -> POS on port 4102)
# ---------------------------------------------------------------------------

class CallbackCollector:
    """Thread-safe accumulator for Channel 1 DeviceRequest messages during an active OPI operation."""

    def __init__(self, on_status: Callable[[str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self.receipt_lines: list[str] = []
        self.merchant_receipt_lines: list[str] = []
        self.customer_receipt_lines: list[str] = []
        self.intermediate_statuses: list[str] = []
        self.e_journals: list[str] = []
        self.on_status = on_status

    def handle_device_request(self, dev_req: ParsedDeviceRequest) -> str:
        """Process a `ParsedDeviceRequest` and return the matching `DeviceResponse` XML."""
        with self._lock:
            target = (dev_req.out_device_target or "").strip()
            ticket_type = (dev_req.ticket_type or "").strip()

            if target == "Printer":
                self.receipt_lines.extend(dev_req.text_lines)
                if ticket_type == "MerchantReceipt":
                    self.merchant_receipt_lines.extend(dev_req.text_lines)
                elif ticket_type == "CustomerReceipt":
                    self.customer_receipt_lines.extend(dev_req.text_lines)
            elif target in ("CashierDisplay", "CustomerDisplay", "PinPad"):
                msg = " ".join(line.strip() for line in dev_req.text_lines if line.strip())
                if msg:
                    self.intermediate_statuses.append(msg)
                    if self.on_status:
                        try:
                            self.on_status(msg)
                        except Exception:
                            pass
            elif dev_req.text_lines:
                self.receipt_lines.extend(dev_req.text_lines)

            if dev_req.e_journal_xml:
                self.e_journals.append(dev_req.e_journal_xml)

        # Build matching DeviceResponse echoing WorkstationID, RequestID, RequestType
        if dev_req.in_device_target:
            # Default input handling: acknowledge with boolean true for GetConfirmation or empty string
            if dev_req.input_command == "GetConfirmation":
                return build_device_response(dev_req, input_boolean=True)
            return build_device_response(dev_req, input_string="")
        return build_device_response(dev_req)


class _Channel1TCPHandler(socketserver.BaseRequestHandler):
    """Handles incoming Channel 1 (`DeviceRequest`) connections from OPI terminals."""

    def handle(self) -> None:
        server: OPICallbackServer = self.server  # type: ignore[assignment]
        sock: socket.socket = self.request
        sock.settimeout(15.0)
        peer_ip = self.client_address[0] if self.client_address else ""

        try:
            while True:
                try:
                    xml_str = read_opi_frame(sock)
                except OPIConnectionError:
                    break
                logger.info("OPI Channel 1 <- DeviceRequest from %s:\n%s", peer_ip, xml_str)
                dev_req = parse_device_request(xml_str)
                resp_xml = server.dispatch_device_request(peer_ip, dev_req)
                logger.info("OPI Channel 1 -> DeviceResponse to %s:\n%s", peer_ip, resp_xml)
                sock.sendall(encode_opi_frame(resp_xml))
        except (socket.timeout, OSError, OPIError) as exc:
            logger.debug("Channel 1 callback connection ended (%s): %s", peer_ip, exc)


class _ReusableThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class OPICallbackServer:
    """
    Channel 1 TCP listener (default port 4102) that receives `<DeviceRequest>` callbacks
    from OPI terminals and replies with `<DeviceResponse>`.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_CHANNEL1_PORT) -> None:
        self._lock = threading.Lock()
        self._collectors_by_ip: dict[str, CallbackCollector] = {}
        self._default_collector: CallbackCollector | None = None
        self._tcp_server = _ReusableThreadingTCPServer((host, port), _Channel1TCPHandler)
        self._tcp_server.dispatch_device_request = self.dispatch_device_request  # type: ignore[attr-defined]
        self.host, self.port = self._tcp_server.server_address
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._tcp_server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("OPI Channel 1 callback listener active on %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._tcp_server.shutdown()
        self._tcp_server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def register_collector(self, terminal_ip: str, collector: CallbackCollector) -> None:
        with self._lock:
            self._collectors_by_ip[terminal_ip] = collector
            self._default_collector = collector

    def unregister_collector(self, terminal_ip: str, collector: CallbackCollector) -> None:
        with self._lock:
            if self._collectors_by_ip.get(terminal_ip) is collector:
                del self._collectors_by_ip[terminal_ip]
            if self._default_collector is collector:
                self._default_collector = None

    def dispatch_device_request(self, peer_ip: str, dev_req: ParsedDeviceRequest) -> str:
        with self._lock:
            collector = self._collectors_by_ip.get(peer_ip) or self._default_collector
        if collector is None:
            collector = CallbackCollector()
        return collector.handle_device_request(dev_req)

    def __enter__(self) -> "OPICallbackServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Channel 0 OPI Client (POS -> Terminal on port 4100)
# ---------------------------------------------------------------------------

class OPIClient:
    """
    TCP client for communicating with an OPI payment terminal on Channel 0 (default port 4100),
    coordinated with an `OPICallbackServer` on Channel 1 (default port 4102).
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_CHANNEL0_PORT,
        timeout: float = 90.0,
        workstation_id: str = "POS-1",
        language_code: str = "de",
        callback_server: OPICallbackServer | None = None,
    ) -> None:
        self.host = host
        # Automatically migrate legacy ZVT default port 20007 to OPI default port 4100 if passed
        self.port = DEFAULT_CHANNEL0_PORT if port == 20007 else (port or DEFAULT_CHANNEL0_PORT)
        self.timeout = timeout
        self.workstation_id = workstation_id
        self.language_code = language_code
        self.callback_server = callback_server

    def _connect(self, connect_timeout: float | None = None) -> socket.socket:
        c_timeout = connect_timeout if connect_timeout is not None else min(self.timeout, 10.0)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=c_timeout)
            sock.settimeout(self.timeout)
            return sock
        except socket.timeout as exc:
            raise OPITimeoutError(
                f"Zeitüberschreitung beim Verbinden mit OPI-Terminal {self.host}:{self.port}"
            ) from exc
        except OSError as exc:
            raise OPIConnectionError(
                f"Verbindung zu OPI-Terminal {self.host}:{self.port} fehlgeschlagen: {exc}"
            ) from exc

    def _send_and_receive(
        self,
        xml_request: str,
        expected_workstation_id: str,
        expected_request_id: str,
        expected_request_type: str,
        amount_cents: int = 0,
        currency: str = "CHF",
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        collector = CallbackCollector(on_status=on_status)
        if self.callback_server:
            self.callback_server.register_collector(self.host, collector)

        sock = self._connect()
        try:
            logger.info(
                "OPI Channel 0 -> %s:%d (%s, RequestID=%s):\n%s",
                self.host,
                self.port,
                expected_request_type,
                expected_request_id,
                xml_request,
            )
            sock.sendall(encode_opi_frame(xml_request))

            # Wait for final CardServiceResponse / ServiceResponse.
            # If the terminal sends any DeviceRequest directly on Channel 0, handle it inline too.
            while True:
                raw_xml = read_opi_frame(sock)
                logger.info(
                    "OPI Channel 0 <- %s:%d:\n%s",
                    self.host,
                    self.port,
                    raw_xml,
                )
                try:
                    root = ET.fromstring(raw_xml)
                except ET.ParseError as exc:
                    raise OPIError(f"Ungültiges XML vom Terminal empfangen: {exc}") from exc

                tag_name = _local_name(root.tag)
                if tag_name == "DeviceRequest":
                    dev_req = parse_device_request(raw_xml)
                    dev_resp_xml = collector.handle_device_request(dev_req)
                    sock.sendall(encode_opi_frame(dev_resp_xml))
                    continue

                resp = parse_opi_response(
                    raw_xml,
                    default_amount_cents=amount_cents,
                    default_currency=currency,
                )

                # Verify correlation attributes (WorkstationID, RequestID, RequestType)
                if resp.request_id and resp.request_id != expected_request_id:
                    raise OPIError(
                        f"OPI RequestID-Konflikt: erwartet '{expected_request_id}', erhalten '{resp.request_id}'"
                    )
                if resp.workstation_id and resp.workstation_id != expected_workstation_id:
                    raise OPIError(
                        f"OPI WorkstationID-Konflikt: erwartet '{expected_workstation_id}', erhalten '{resp.workstation_id}'"
                    )
                if resp.request_type and resp.request_type != expected_request_type:
                    raise OPIError(
                        f"OPI RequestType-Konflikt: erwartet '{expected_request_type}', erhalten '{resp.request_type}'"
                    )

                resp.receipt_lines = list(collector.receipt_lines)
                resp.merchant_receipt_lines = list(collector.merchant_receipt_lines)
                resp.customer_receipt_lines = list(collector.customer_receipt_lines)
                resp.intermediate_statuses = list(collector.intermediate_statuses)
                if collector.e_journals:
                    resp.terminal_info["e_journals"] = list(collector.e_journals)
                return resp

        except socket.timeout as exc:
            raise OPITimeoutError(
                f"Zeitüberschreitung beim Warten auf Antwort von OPI-Terminal {self.host}:{self.port} "
                f"(RequestID={expected_request_id}). Bitte Transaktionsstatus prüfen (RepeatLastMessage)."
            ) from exc
        except OSError as exc:
            raise OPIConnectionError(
                f"Kommunikationsfehler mit OPI-Terminal {self.host}:{self.port} "
                f"(RequestID={expected_request_id}): {exc}"
            ) from exc
        finally:
            if self.callback_server:
                self.callback_server.unregister_collector(self.host, collector)
            try:
                sock.close()
            except OSError:
                pass

    def authorize(
        self,
        amount_cents: int,
        currency: str = "CHF",
        reference_number: str | None = None,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """Send OPI `CardServiceRequest` with `RequestType="CardPayment"`."""
        xml_req, req_id = build_card_service_request(
            request_type="CardPayment",
            workstation_id=self.workstation_id,
            request_id=request_id,
            amount_cents=amount_cents,
            currency=currency,
            reference_number=reference_number or f"ORDER-{req_id if (req_id := request_id) else 'POS'}",
            language_code=self.language_code,
        )
        return self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type="CardPayment",
            amount_cents=amount_cents,
            currency=currency,
            on_status=on_status,
        )

    def refund(
        self,
        amount_cents: int,
        currency: str = "CHF",
        reference_number: str | None = None,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """Send standalone OPI `CardServiceRequest` with `RequestType="PaymentRefund"`."""
        xml_req, req_id = build_card_service_request(
            request_type="PaymentRefund",
            workstation_id=self.workstation_id,
            request_id=request_id,
            amount_cents=amount_cents,
            currency=currency,
            reference_number=reference_number,
            language_code=self.language_code,
        )
        return self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type="PaymentRefund",
            amount_cents=amount_cents,
            currency=currency,
            on_status=on_status,
        )

    def reversal(
        self,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """Send OPI `CardServiceRequest` with `RequestType="PaymentReversal"` to reverse the last transaction."""
        xml_req, req_id = build_card_service_request(
            request_type="PaymentReversal",
            workstation_id=self.workstation_id,
            request_id=request_id,
            language_code=self.language_code,
        )
        return self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type="PaymentReversal",
            on_status=on_status,
        )

    def reprint(
        self,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """Send OPI `CardServiceRequest` with `RequestType="TicketReprint"`."""
        xml_req, req_id = build_card_service_request(
            request_type="TicketReprint",
            workstation_id=self.workstation_id,
            request_id=request_id,
            language_code=self.language_code,
        )
        return self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type="TicketReprint",
            on_status=on_status,
        )

    def recover_last_transaction(
        self,
        expected_original_request_id: str | None = None,
        expected_original_request_type: str = "CardPayment",
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """
        Send OPI `CardServiceRequest` with `RequestType="RepeatLastMessage"` to recover an interrupted transaction.
        Validates `OriginalHeader` against the interrupted transaction before accepting its `OverallResult`.
        """
        xml_req, req_id = build_card_service_request(
            request_type="RepeatLastMessage",
            workstation_id=self.workstation_id,
            request_id=request_id,
            language_code=self.language_code,
        )
        resp = self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type="RepeatLastMessage",
            on_status=on_status,
        )
        if not resp.success or not resp.original_header:
            resp.success = False
            resp.status = "unknown"
            resp.result_message = "Wiederherstellung lieferte keinen passenden OriginalHeader – Status bleibt unbekannt"
            return resp

        orig = resp.original_header
        orig_ws = orig.get("WorkstationID", "")
        orig_req_id = orig.get("RequestID", "")
        orig_type = orig.get("RequestType", "")
        orig_result = orig.get("OverallResult", "")

        if orig_ws and orig_ws != self.workstation_id:
            resp.success = False
            resp.status = "unknown"
            resp.result_message = f"OriginalHeader WorkstationID ({orig_ws}) stimmt nicht mit {self.workstation_id} überein"
            return resp

        if expected_original_request_id and orig_req_id != str(expected_original_request_id):
            resp.success = False
            resp.status = "unknown"
            resp.result_message = (
                f"OriginalHeader RequestID ({orig_req_id}) stimmt nicht mit erwarteter RequestID "
                f"({expected_original_request_id}) überein"
            )
            return resp

        if expected_original_request_type and orig_type and orig_type != expected_original_request_type:
            resp.success = False
            resp.status = "unknown"
            resp.result_message = (
                f"OriginalHeader RequestType ({orig_type}) stimmt nicht mit {expected_original_request_type} überein"
            )
            return resp

        # Apply the recovered transaction's OverallResult
        host_decline = int(resp.diagnosis.get("HostDeclineReason") or 0) if resp.diagnosis else 0
        term_decline = int(resp.diagnosis.get("TerminalDeclineReason") or 0) if resp.diagnosis else 0
        ok, st, code, msg = _describe_opi_outcome(orig_result, orig_type or expected_original_request_type, host_decline, term_decline)
        resp.success = ok
        resp.status = st
        resp.overall_result = orig_result
        resp.result_code = code
        resp.result_message = f"{msg} (Wiederhergestellt via RepeatLastMessage)"
        return resp

    def abort(self, request_id: str | None = None) -> OPIResponse:
        """
        Send OPI `CardServiceRequest` with `RequestType="AbortRequest"` on a separate TCP connection.
        Note: Per Nexi OPI docs, an abort acknowledgement only confirms the abort command;
        the original payment connection still determines the financial result.
        """
        xml_req, req_id = build_card_service_request(
            request_type="AbortRequest",
            workstation_id=self.workstation_id,
            request_id=request_id,
            language_code=self.language_code,
            request_transaction_info=False,
        )
        sock = self._connect(connect_timeout=5.0)
        try:
            logger.info("OPI Channel 0 (Abort) -> %s:%d:\n%s", self.host, self.port, xml_req)
            sock.sendall(encode_opi_frame(xml_req))
            try:
                raw_xml = read_opi_frame(sock)
                logger.info("OPI Channel 0 (Abort) <- %s:%d:\n%s", self.host, self.port, raw_xml)
                resp = parse_opi_response(raw_xml)
                if resp.overall_result == "Success":
                    resp.status = "ok"
                    resp.result_message = "Abbruchbefehl vom Terminal bestätigt"
                return resp
            except (socket.timeout, OPIConnectionError):
                # Some terminals close the abort socket immediately after processing AbortRequest
                return OPIResponse(
                    success=True,
                    status="ok",
                    overall_result="Success",
                    result_code=0,
                    result_message="Abbruchbefehl gesendet",
                    workstation_id=self.workstation_id,
                    request_id=req_id,
                    request_type="AbortRequest",
                )
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def service_operation(
        self,
        request_type: str,
        request_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> OPIResponse:
        """
        Execute an OPI `<ServiceRequest>` (`Login`, `Logoff`, `GetStatus`, `GetInfo`,
        `CloseDay`, `TransmitTrx`, `ContactTMS`, `ContactAcq`).
        """
        xml_req, req_id = build_service_request(
            request_type=request_type,
            workstation_id=self.workstation_id,
            request_id=request_id,
            language_code=self.language_code,
        )
        resp = self._send_and_receive(
            xml_req,
            expected_workstation_id=self.workstation_id,
            expected_request_id=req_id,
            expected_request_type=request_type,
            on_status=on_status,
        )
        if resp.success:
            messages = {
                "Login": "Terminal erfolgreich angemeldet (Login)",
                "Logoff": "Terminal erfolgreich abgemeldet (Logoff)",
                "GetStatus": "Terminal bereit",
                "GetInfo": "Terminal-Informationen abgerufen",
                "CloseDay": "Tagesabschluss erfolgreich durchgeführt",
                "TransmitTrx": "Transaktionen erfolgreich übertragen",
                "ContactTMS": "Terminal-Konfiguration (ContactTMS) erfolgreich aktualisiert",
                "ContactAcq": "Acquirer-Initialisierung (ContactAcq) erfolgreich durchgeführt",
            }
            resp.result_message = messages.get(request_type, resp.result_message)
        return resp

    def status(self) -> OPIResponse:
        """Send OPI `ServiceRequest` with `RequestType="GetStatus"`."""
        return self.service_operation("GetStatus")

    def register(self, **kwargs: Any) -> OPIResponse:
        """Send OPI `ServiceRequest` with `RequestType="Login"`."""
        return self.service_operation("Login")

    def login(self) -> OPIResponse:
        """Send OPI `ServiceRequest` with `RequestType="Login"`."""
        return self.service_operation("Login")

    def close_day(self) -> OPIResponse:
        """Send OPI `ServiceRequest` with `RequestType="CloseDay"`."""
        return self.service_operation("CloseDay")
