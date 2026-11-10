# Terminalorama

Lightweight, zero-dependency Python 3.12 HTTP/JSON server that bridges **Kassorama** (or any POS/ECR) with Swiss credit card terminals using the **OPI protocol (Open Payment Interface)** over TCP/IP, following the [Nexi Switzerland OPI specification](https://docs.nexi.swiss/de/opi-integration).

## Architecture & OPI Two-Channel Model

- **Framing**: Every OPI XML message over TCP uses a **4-byte big-endian byte length prefix** followed by the UTF-8 encoded XML payload (`[4-byte big-endian length][utf-8 xml payload]`).
- **XML Namespace**: `http://www.nrf-arts.org/IXRetail/namespace`
- **Channel 0 (`POS -> Terminal`, default TCP port `4100`)**: Sends `CardServiceRequest` (`CardPayment`, `PaymentRefund`, `PaymentReversal`, `TicketReprint`, `RepeatLastMessage`, `AbortRequest`) and `ServiceRequest` (`Login`, `Logoff`, `GetStatus`, `GetInfo`, `CloseDay`, `TransmitTrx`, `ContactTMS`, `ContactAcq`), and receives `CardServiceResponse` / `ServiceResponse`.
- **Channel 1 (`Terminal -> POS`, default TCP port `4102`)**: Background `OPICallbackServer` listener that accepts terminal-initiated `DeviceRequest` callbacks (`Printer`, `CashierDisplay`, `CustomerDisplay`, `E-Journal`, `CashierKeyboard`), captures receipt lines (`CustomerReceipt` / `MerchantReceipt`) and display prompts, and replies with matching `DeviceResponse` messages preserving `WorkstationID`, `RequestID` (e.g. `2001.1`), and `RequestType`.

## Starting the Server

```bash
cd /home/outpost/code/terminalorama
python3 -m terminalorama --host 127.0.0.1 --port 8080 --callback-port 4102
```

To run in **Mock Mode** (simulates OPI terminal responses without physical hardware):

```bash
python3 -m terminalorama --host 127.0.0.1 --port 8080 --mock
```
*(You can also set a Cash Register's Terminal IP in Kassorama to `mock` to use the simulator on a per-register basis.)*

## HTTP/JSON API

### `GET /health`
Check server health and active Channel 1 callback port.

### `POST /pay`
Initiate an OPI `CardServiceRequest` (`RequestType="CardPayment"`) on the specified terminal.
- Automatically handles `OverallResult="Loggedout"` by sending `ServiceRequest` (`Login`) first and retrying with a fresh `RequestID`.
- Automatically attempts `RepeatLastMessage` recovery if a socket timeout occurs after sending the payment request.

**Request:**
```json
{
  "ip": "192.168.1.11",
  "port": 4100,
  "amount": 25.50,
  "currency": "CHF",
  "reference_number": "ORDER-1001",
  "timeout": 90
}
```
- Use `"ip": "mock"` or `"mock": true` to simulate an OPI terminal without hardware.
- In mock mode:
  - `99.95` CHF simulates a customer abort (`OverallResult="Aborted"`, `TerminalDeclineReason="523"`).
  - `99.90` CHF simulates a card decline (`OverallResult="Failure"`, `HostDeclineReason="51"`).

**Response (`200 OK` on approval, `422` on decline/abort):**
```json
{
  "success": true,
  "status": "approved",
  "overall_result": "Success",
  "result_code": 0,
  "result_message": "Zahlung erfolgreich",
  "amount_cents": 2550,
  "currency": "CHF",
  "workstation_id": "POS-1",
  "request_id": "2001",
  "request_type": "CardPayment",
  "receipt_lines": ["..."],
  "merchant_receipt_lines": ["..."],
  "customer_receipt_lines": ["..."],
  "intermediate_statuses": ["Bitte Karte vorhalten"],
  "terminal_info": {
    "terminal_id": "12345678",
    "stan": "100001",
    "approval_code": "123456",
    "trx_reference_number": "384100001",
    "card_circuit": "ECMC",
    "masked_card_number": "xxxxxxxxxxxx1234"
  },
  "diagnosis": {
    "Module": "EP2",
    "HostDeclineReason": "0",
    "TerminalDeclineReason": "0"
  }
}
```

### Additional OPI Endpoints
- `POST /refund` — OPI `CardServiceRequest` (`RequestType="PaymentRefund"`)
- `POST /reversal` — OPI `CardServiceRequest` (`RequestType="PaymentReversal"`)
- `POST /reprint` — OPI `CardServiceRequest` (`RequestType="TicketReprint"`)
- `POST /recover` — OPI `CardServiceRequest` (`RequestType="RepeatLastMessage"`)
- `POST /abort` — OPI `CardServiceRequest` (`RequestType="AbortRequest"`, sent on a separate TCP connection)
- `POST /status` — OPI `ServiceRequest` (`RequestType="GetStatus"`)
- `POST /login` (or `POST /register`) — OPI `ServiceRequest` (`RequestType="Login"`)
- `POST /close_day` — OPI `ServiceRequest` (`RequestType="CloseDay"`)

## Running Tests

```bash
cd /home/outpost/code/terminalorama
python3 -m unittest discover -s tests -v
```
