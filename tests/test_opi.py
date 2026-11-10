"""Unit and integration tests for Terminalorama OPI protocol (Channel 0 & Channel 1) and HTTP API."""

from __future__ import annotations

import json
import threading
import unittest
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from terminalorama.mock_terminal import MockOPIServer
from terminalorama.opi import (
    OPI_NAMESPACE,
    OPICallbackServer,
    OPIClient,
    build_card_service_request,
    build_device_response,
    build_service_request,
    encode_opi_frame,
    parse_device_request,
    parse_opi_frame,
    parse_opi_response,
)
from terminalorama.server import TerminaloramaHTTPServer, TerminalRegistry


class TestOPIProtocol(unittest.TestCase):
    def test_four_byte_length_framing(self) -> None:
        xml_str = '<?xml version="1.0" encoding="utf-8"?>\n<ServiceRequest />'
        framed = encode_opi_frame(xml_str)
        # First 4 bytes must be big-endian length of UTF-8 payload
        expected_len = len(xml_str.encode("utf-8"))
        self.assertEqual(int.from_bytes(framed[:4], "big"), expected_len)

        decoded, rem = parse_opi_frame(framed + b"extra")
        self.assertEqual(decoded, xml_str)
        self.assertEqual(rem, b"extra")

    def test_build_card_payment_xml(self) -> None:
        xml_str, req_id = build_card_service_request(
            request_type="CardPayment",
            workstation_id="POS-1",
            request_id="2001",
            amount_cents=4990,
            currency="CHF",
            reference_number="ORDER-2001",
            language_code="de",
            shift_number="1",
            clerk_id="12",
        )
        self.assertEqual(req_id, "2001")
        root = ET.fromstring(xml_str)
        self.assertTrue(root.tag.endswith("CardServiceRequest"))
        self.assertEqual(root.attrib["WorkstationID"], "POS-1")
        self.assertEqual(root.attrib["RequestID"], "2001")
        self.assertEqual(root.attrib["RequestType"], "CardPayment")
        self.assertEqual(root.attrib["ReferenceNumber"], "ORDER-2001")

        # Verify mandatory POSdata status fields and TotalAmount
        ns = {"opi": OPI_NAMESPACE}
        pos_data = root.find("opi:POSdata", ns)
        self.assertIsNotNone(pos_data)
        self.assertEqual(pos_data.attrib["LanguageCode"], "de")
        self.assertEqual(pos_data.attrib["RequestTransactionInformation"], "true")
        self.assertEqual(pos_data.findtext("opi:PrinterStatus", namespaces=ns), "Available")
        self.assertEqual(pos_data.findtext("opi:E-JournalStatus", namespaces=ns), "Available")
        self.assertEqual(pos_data.findtext("opi:JournalPrinterStatus", namespaces=ns), "Available")

        total_el = root.find("opi:TotalAmount", ns)
        self.assertIsNotNone(total_el)
        self.assertEqual(total_el.attrib["Currency"], "CHF")
        self.assertEqual(total_el.text, "49.90")

    def test_parse_card_service_response_and_diagnosis(self) -> None:
        approved_xml = """<?xml version="1.0" encoding="utf-8"?>
<CardServiceResponse WorkstationID="POS-1"
                     RequestID="2001"
                     RequestType="CardPayment"
                     OverallResult="Success"
                     xmlns="http://www.nrf-arts.org/IXRetail/namespace">
  <Terminal TerminalID="12345678" STAN="123456" />
  <Tender LanguageCode="en">
    <TotalAmount Currency="CHF">49.90</TotalAmount>
    <Authorisation ApprovalCode="123456"
                   TrxReferenceNumber="384999238"
                   CardCircuit="ECMC"
                   MaskedCardNumber="xxxxxxxxxxxx1234" />
  </Tender>
  <Diagnosis Module="EP2" HostDeclineReason="0" TerminalDeclineReason="0" />
</CardServiceResponse>"""
        resp = parse_opi_response(approved_xml)
        self.assertTrue(resp.success)
        self.assertEqual(resp.status, "approved")
        self.assertEqual(resp.amount_cents, 4990)
        self.assertEqual(resp.currency, "CHF")
        self.assertEqual(resp.terminal_info["terminal_id"], "12345678")
        self.assertEqual(resp.terminal_info["stan"], "123456")
        self.assertEqual(resp.terminal_info["approval_code"], "123456")
        self.assertEqual(resp.terminal_info["trx_reference_number"], "384999238")

        declined_xml = """<?xml version="1.0" encoding="utf-8"?>
<CardServiceResponse WorkstationID="POS-1"
                     RequestID="2002"
                     RequestType="CardPayment"
                     OverallResult="Failure"
                     xmlns="http://www.nrf-arts.org/IXRetail/namespace">
  <Terminal TerminalID="12345678" STAN="123457" />
  <Diagnosis Module="EP2" HostDeclineReason="55" TerminalDeclineReason="0" />
</CardServiceResponse>"""
        dec_resp = parse_opi_response(declined_xml)
        self.assertFalse(dec_resp.success)
        self.assertEqual(dec_resp.status, "declined")
        self.assertEqual(dec_resp.result_code, 55)
        self.assertIn("Falsche PIN", dec_resp.result_message)

    def test_device_request_and_response_callback_correlation(self) -> None:
        callback_xml = """<?xml version="1.0" encoding="utf-8"?>
<DeviceRequest WorkstationID="POS-1"
               RequestID="2001.1"
               RequestType="Output"
               SequenceID="1"
               xmlns="http://www.nrf-arts.org/IXRetail/namespace">
  <Output OutDeviceTarget="Printer" TicketType="CustomerReceipt">
    <TextLine>CUSTOMER RECEIPT</TextLine>
    <TextLine>AMOUNT CHF 49.90</TextLine>
  </Output>
</DeviceRequest>"""
        dev_req = parse_device_request(callback_xml)
        self.assertEqual(dev_req.workstation_id, "POS-1")
        self.assertEqual(dev_req.request_id, "2001.1")
        self.assertEqual(dev_req.request_type, "Output")
        self.assertEqual(dev_req.out_device_target, "Printer")
        self.assertEqual(dev_req.ticket_type, "CustomerReceipt")
        self.assertEqual(dev_req.text_lines, ["CUSTOMER RECEIPT", "AMOUNT CHF 49.90"])

        resp_xml = build_device_response(dev_req)
        resp_root = ET.fromstring(resp_xml)
        # Must preserve callback RequestID="2001.1", not payment RequestID="2001"
        self.assertEqual(resp_root.attrib["WorkstationID"], "POS-1")
        self.assertEqual(resp_root.attrib["RequestID"], "2001.1")
        self.assertEqual(resp_root.attrib["RequestType"], "Output")
        self.assertEqual(resp_root.attrib["OverallResult"], "Success")

    def test_build_service_request(self) -> None:
        xml_str, req_id = build_service_request("Login", workstation_id="POS-1", request_id="2501")
        self.assertEqual(req_id, "2501")
        root = ET.fromstring(xml_str)
        self.assertTrue(root.tag.endswith("ServiceRequest"))
        self.assertEqual(root.attrib["RequestType"], "Login")


class TestOPITCPIntegration(unittest.TestCase):
    def test_opi_two_channel_exchange_against_mock_tcp_server(self) -> None:
        with OPICallbackServer(host="127.0.0.1", port=0) as cb_server:
            with MockOPIServer(
                host="127.0.0.1",
                port=0,
                callback_host="127.0.0.1",
                callback_port=cb_server.port,
            ) as opi_terminal:
                client = OPIClient(
                    host=opi_terminal.host,
                    port=opi_terminal.port,
                    timeout=5.0,
                    workstation_id="POS-1",
                    callback_server=cb_server,
                )

                # 1. Service: GetStatus
                st_resp = client.status()
                self.assertTrue(st_resp.success)
                self.assertEqual(st_resp.status, "ok")

                # 2. Service: Login
                login_resp = client.login()
                self.assertTrue(login_resp.success)

                # 3. CardPayment (with Channel 1 display + receipt callbacks)
                pay_resp = client.authorize(amount_cents=2550, currency="CHF", request_id="3001")
                self.assertTrue(pay_resp.success)
                self.assertEqual(pay_resp.status, "approved")
                self.assertEqual(pay_resp.amount_cents, 2550)
                self.assertIn("Bitte Karte vorhalten", pay_resp.intermediate_statuses)
                self.assertTrue(any("BETRAG CHF 25.50" in line for line in pay_resp.receipt_lines))
                self.assertTrue(any("BETRAG CHF 25.50" in line for line in pay_resp.customer_receipt_lines))

                # 4. RepeatLastMessage recovery
                rec_resp = client.recover_last_transaction(
                    expected_original_request_id="3001",
                    expected_original_request_type="CardPayment",
                )
                self.assertTrue(rec_resp.success)
                self.assertEqual(rec_resp.status, "approved")

                # 5. Aborted payment (99.95 CHF trigger -> Diagnosis 523)
                abort_resp = client.authorize(amount_cents=9995, currency="CHF", request_id="3002")
                self.assertFalse(abort_resp.success)
                self.assertEqual(abort_resp.status, "aborted")
                self.assertEqual(abort_resp.result_code, 523)

                # 6. Declined payment (99.90 CHF trigger -> Diagnosis 51)
                dec_resp = client.authorize(amount_cents=9990, currency="CHF", request_id="3003")
                self.assertFalse(dec_resp.success)
                self.assertEqual(dec_resp.status, "declined")
                self.assertEqual(dec_resp.result_code, 51)


class TestHTTPServerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.cb_server = OPICallbackServer(host="127.0.0.1", port=0)
        self.cb_server.start()

        self.opi_terminal = MockOPIServer(
            host="127.0.0.1",
            port=0,
            callback_host="127.0.0.1",
            callback_port=self.cb_server.port,
        )
        self.opi_terminal.start()

        registry = TerminalRegistry(callback_server=self.cb_server)
        self.http_server = TerminaloramaHTTPServer(("127.0.0.1", 0), registry=registry)
        self.http_host, self.http_port = self.http_server.server_address
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        self.base_url = f"http://{self.http_host}:{self.http_port}"

    def tearDown(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.opi_terminal.stop()
        self.cb_server.stop()

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        req = Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_health_endpoint(self) -> None:
        with urlopen(f"{self.base_url}/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["protocol"], "OPI")

    def test_pay_over_opi_tcp(self) -> None:
        code, data = self._post(
            "/pay",
            {
                "ip": self.opi_terminal.host,
                "port": self.opi_terminal.port,
                "amount": 19.90,
                "currency": "CHF",
            },
        )
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["amount_cents"], 1990)
        self.assertTrue(any("BETRAG CHF 19.90" in line for line in data["receipt_lines"]))

    def test_pay_aborted_over_opi_tcp(self) -> None:
        code, data = self._post(
            "/pay",
            {
                "ip": self.opi_terminal.host,
                "port": self.opi_terminal.port,
                "amount": 99.95,
                "currency": "CHF",
            },
        )
        self.assertEqual(code, 422)
        self.assertFalse(data["success"])
        self.assertEqual(data["status"], "aborted")
        self.assertEqual(data["result_code"], 523)

    def test_pay_with_in_process_mock(self) -> None:
        code, data = self._post(
            "/pay",
            {
                "ip": "mock",
                "amount": 42.00,
                "currency": "CHF",
            },
        )
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["amount_cents"], 4200)


if __name__ == "__main__":
    unittest.main()

