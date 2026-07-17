"""BLEManager — bleak central: scan / connect / read / write / subscribe."""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from .constants import *

class BLEManager:
    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.connected = False
        # Legacy single-callback slot. Still honoured for back-compat with
        # ConnectionTab, but new code should use add_disconnect_sub() so
        # multiple tabs can react to a disconnect independently.
        self.on_disconnect_cb = None
        self._disconnect_subs: List = []
        self.log_cb = None
        # Optional global EventLog — set by App right after construction.
        self.event_log = None
        # Last MAC the user successfully connected to. Persists across
        # disconnects so the "Reconnect" button in the header has somewhere
        # to go without forcing another scan.
        self.last_address: Optional[str] = None
        self.last_name: Optional[str] = None

    def _log(self, msg: str):
        if self.log_cb:
            self.log_cb(msg)

    def _evt(self, kind: str, message: str):
        """Forward to global EventLog if wired; silent otherwise."""
        if self.event_log is not None:
            try:
                self.event_log.log(kind, "ble", message)
            except Exception:
                pass

    def add_disconnect_sub(self, cb):
        """Register an additional callback for disconnect events."""
        if callable(cb) and cb not in self._disconnect_subs:
            self._disconnect_subs.append(cb)

    def _on_disconnect(self, _client):
        self.connected = False
        self._log("Disconnected from device.")
        self._evt("disconnect", "device disconnected")
        if self.on_disconnect_cb:
            try:
                self.on_disconnect_cb()
            except Exception as e:
                self._log(f"on_disconnect_cb error: {e}")
        for sub in list(self._disconnect_subs):
            try:
                sub()
            except Exception as e:
                self._log(f"disconnect sub error: {e}")

    async def scan(self, timeout: float = 8.0, show_all: bool = False,
                    on_update: Optional[Callable[[dict], None]] = None) -> List[dict]:
        """
        Streaming-mode scan: opens a BleakScanner and accumulates every
        advertisement that arrives during `timeout` seconds. More reliable
        than BleakScanner.discover() on Windows — that batch API occasionally
        drops legitimate adverts even when the OS Bluetooth stack receives
        them.

        If `on_update` is given, it is invoked for every adv (after dedupe
        and rssi-update) with the same result-dict shape as the final
        return list.  The UI uses this to populate its tree live instead
        of waiting for the timeout to elapse.  The callback runs on the
        BLE thread — wrap UI mutations in `widget.after(0, ...)`.

        Every individual advertisement is logged to the EventLog ("scan_adv")
        so you can confirm in real time whether bleak is actually seeing
        your device. If the EventLog shows the device's address arriving
        but it doesn't appear in the result list, the bug is in this method;
        if no scan_adv lines appear at all, bleak / the OS Bluetooth stack
        isn't seeing it (try restarting Bluetooth, scanning longer, or using
        nRF Connect to confirm).
        """
        _ = show_all  # accepted for backward compat; everything is shown
        self._log(f"Scanning for {timeout} s … (streaming, all devices)")
        self._evt("scan", f"start: timeout={timeout}s (streaming)")

        target_uuid = SVC_AUTH.lower()
        discovered: Dict[str, Tuple[Any, Any]] = {}

        def _make_entry(dev, adv) -> dict:
            name = dev.name or adv.local_name or ""
            adv_uuids = [u.lower() for u in (adv.service_uuids or [])]
            is_target = (
                (bool(name) and DEVICE_NAME.lower() in name.lower())
                or (target_uuid in adv_uuids)
            )
            display_name = name
            if not display_name:
                if adv.manufacturer_data:
                    mfr_id, mfr_bytes = next(iter(adv.manufacturer_data.items()))
                    display_name = f"<mfr 0x{mfr_id:04X}: {mfr_bytes[:4].hex()}…>"
                elif adv.service_uuids:
                    display_name = f"<svc {adv.service_uuids[0][:8]}…>"
                else:
                    display_name = "<no name>"
            return {
                "name":    display_name,
                "address": dev.address,
                "rssi":    adv.rssi if adv.rssi is not None else -999,
                "tag":     " [TARGET]" if is_target else "",
                "device":  dev,
            }

        def _on_adv(device, adv):
            # Fires for EVERY advertisement received, including duplicates.
            # We dedupe by address but log each hit for visibility, and
            # push a live update to the UI on every hit so the tree
            # reflects current RSSI rather than first-seen RSSI.
            try:
                addr = device.address
            except Exception:
                return
            name = (device.name or adv.local_name or "").strip()
            rssi = adv.rssi if adv.rssi is not None else -999
            short_uuids = [u for u in (adv.service_uuids or [])][:1]
            self._evt("scan",
                       f"adv  {addr}  rssi={rssi:>4} dBm  "
                       f"name='{name or '<none>'}'  "
                       f"svc={short_uuids[0][:8] if short_uuids else '-'}")
            discovered[addr] = (device, adv)
            if on_update is not None:
                try:
                    on_update(_make_entry(device, adv))
                except Exception as cb_err:
                    self._evt("error", f"scan on_update callback failed: {cb_err}")

        try:
            scanner = BleakScanner(detection_callback=_on_adv)
            await scanner.start()
            try:
                await asyncio.sleep(timeout)
            finally:
                await scanner.stop()
        except Exception as e:
            self._evt("error", f"scan failed: {e}")
            self._log(f"Scan failed: {e}")
            raise

        results = [_make_entry(dev, adv) for dev, adv in discovered.values()]
        results.sort(key=lambda d: d["rssi"], reverse=True)
        targets = sum(1 for d in results if d["tag"])
        self._log(f"Found {len(results)} device(s) (streaming, no filter).")
        self._evt("scan",
                   f"done: {len(results)} unique device(s), "
                   f"{targets} tagged TARGET")
        return results

    async def connect(self, device_or_addr) -> bool:
        addr_str = (device_or_addr if isinstance(device_or_addr, str)
                     else getattr(device_or_addr, "address", str(device_or_addr)))
        name = (getattr(device_or_addr, "name", None)
                if not isinstance(device_or_addr, str) else None) or ""
        self._log(f"Connecting …")
        self._evt("connect", f"begin → {addr_str}")
        # winrt use_cached_services=False -> BluetoothCacheMode.UNCACHED, forcing a
        # fresh GATT discovery from the device on every connect. Without it, after
        # re-flashing firmware that changed the GATT table, WinRT keeps serving a
        # STALE cached table whose characteristics look like they have no CCCD, so
        # every start_notify() fails with "characteristic does not support
        # notifications or indications" even though reads/writes work fine. Ignored
        # on BlueZ/macOS backends, so it is safe to pass unconditionally.
        self.client = BleakClient(device_or_addr,
                                  disconnected_callback=self._on_disconnect,
                                  timeout=20.0,
                                  winrt={"use_cached_services": False})
        try:
            await self.client.connect(timeout=20.0)
        except Exception as e:
            self._evt("error", f"connect failed: {e}")
            raise
        self.connected = True
        # Remember for the header's Reconnect button.
        self.last_address = addr_str
        self.last_name = name or self.last_name or ""
        self._log(f"Connected. MTU={self.client.mtu_size}")
        self._evt("connect", f"ok  MTU={self.client.mtu_size}  addr={addr_str}")
        # DIAGNOSTIC: dump the properties bleak actually discovered for the
        # notifiable chars. bleak raises "does not support notifications" from its
        # OWN check of these .properties, so this tells us definitively whether
        # 'notify' is present in what the OS handed us (missing -> stale OS GATT
        # cache / discovery; present -> a CCCD/descriptor problem instead).
        try:
            watch = ("1526", "1529", "1533", "2a19", "152b", "152e")
            for svc in (self.client.services or []):
                for ch in svc.characteristics:
                    short = ch.uuid.lower()[4:8]
                    if short in watch:
                        self._evt("gatt",
                                  f"{short} props={sorted(ch.properties)} "
                                  f"cccd={'yes' if any(d.uuid.lower()[4:8]=='2902' for d in ch.descriptors) else 'no'}")
        except Exception as e:
            self._evt("gatt", f"props dump failed: {e}")
        return True

    async def disconnect(self):
        if self.client and self.connected:
            self._evt("disconnect", "explicit disconnect")
            await self.client.disconnect()
        self.connected = False

    async def read(self, uuid: str) -> bytes:
        try:
            data = bytes(await self.client.read_gatt_char(uuid))
        except Exception as e:
            self._evt("error", f"read {uuid[:8]} failed: {e}")
            raise
        self._evt("read", f"{uuid[:8]}…  {len(data)} B  {data.hex(' ')}")
        return data

    async def write(self, uuid: str, data: bytes, response: bool = True):
        try:
            await self.client.write_gatt_char(uuid, data, response=response)
        except Exception as e:
            self._evt("error", f"write {uuid[:8]} failed: {e}")
            raise
        self._evt("write",
                   f"{uuid[:8]}…  {len(data)} B  {data.hex(' ')}"
                   f"{'  (no-rsp)' if not response else ''}")

    async def subscribe(self, uuid: str, cb):
        try:
            await self.client.start_notify(uuid, cb)
        except Exception as e:
            self._evt("error", f"subscribe {uuid[:8]} failed: {e}")
            raise
        self._evt("subscribe", f"{uuid[:8]}… CCCD on")

    async def unsubscribe(self, uuid: str):
        self._evt("unsubscribe", f"{uuid[:8]}… CCCD off")
        try:
            await self.client.stop_notify(uuid)
        except Exception:
            pass
