"""Protocol constants: UUIDs, geometry, event/reset/fault tables, MSD layout."""

# ─────────────────────────────────────────────────────────────────────────────
#  BLE UUID constants
#
#  Confirmed advertised UUID (verified via nRF Connect for Mobile, scan view):
#      00001523-bcea-5f78-2315-deef12120000
#
#  → the SoftDevice exposes the 16-bit short uuid in the FIRST 4 hex digits of
#    the 128-bit string, with the two preceding bytes left as 0x0000.
#  → general layout for short 0xSSSS:
#       0000SSSS-bcea-5f78-2315-deef12120000
#
#  This matches the firmware base UUID where bytes [0..1] are the placeholder
#  the SoftDevice overwrites with the short — see src/config/ble_config.h.
# ─────────────────────────────────────────────────────────────────────────────

def _uuid(short: int) -> str:
    return f"0000{short:04x}-bcea-5f78-2315-deef12120000"

# Auth Service (0x1523)
SVC_AUTH          = _uuid(0x1523)
CHAR_AUTH_PIN     = _uuid(0x1524)   # Write + Notify — write 3-byte PIN {0,0,0}

# Sensor Service (0x1525) — firmware exposes ONE merged 6-byte char.
# Layout (little-endian): [temp:int16 tenths-degC, db:uint8, peak_db:uint8, lux:uint16]
# Error markers: temp=0x7FFF (INT16_MAX), db=0xFF, lux=0xFFFF.
# Flash records (152D) still carry temp as int8 degC; only this BLE-live char widened.
SVC_SENSOR        = _uuid(0x1525)
CHAR_SENSOR_DATA  = _uuid(0x1526)   # Read + Notify — 6-byte packed reading
CHAR_STATUS       = _uuid(0x1529)   # Read + Notify — 10-byte packed status

# Flash Data Service (0x152A) — sensor blocks AND journal share this service.
SVC_FLASH            = _uuid(0x152A)
CHAR_BLOCK_COUNT     = _uuid(0x152B)   # Read — uint16 LE, unsynced sensor blocks
# Sensor record stream is W + N on the SAME char: subscribe first, then write
# 0x01 — the device fires 152-byte block notifications on the same UUID.
CHAR_RECORD          = _uuid(0x152D)
CHAR_TIMESYNC        = _uuid(0x152E)   # Read+Write — uint32 LE Unix epoch (IST wall-clock); read = device's current epoch (uptime secs until first sync)

# The device is timezone-agnostic: it stores whatever epoch we send and reports it
# back verbatim (journal, advert last-sync, event log). We send India Standard Time
# (UTC+05:30) so the device's wall clock — and every epoch it echoes — reads in IST.
# Bias the host UTC epoch by +05:30 on send; render device epochs with tz=utc so the
# already-biased value prints as the IST wall clock.
IST_OFFSET_S = 5 * 3600 + 30 * 60   # +05:30

# Journal characteristics (event log: BOOT / TIME_SYNC / FLASH_WRAP / ERROR / …)
# Trigger and notify are on DIFFERENT chars: subscribe to RECORD, then write
# 0x01 to START.
CHAR_JOURNAL_COUNT   = _uuid(0x1534)   # Read — uint16 LE valid entry count
CHAR_JOURNAL_RECORD  = _uuid(0x1535)   # Notify — 32-byte JournalEntry stream
CHAR_JOURNAL_START   = _uuid(0x1536)   # Write — 0x01 starts journal transfer

# Haptic Motor Service (0x152F)
SVC_HAPTIC        = _uuid(0x152F)
CHAR_HAPTIC_CTL      = _uuid(0x1530)   # Write — 0x01=ON, else OFF
CHAR_HAPTIC_INT      = _uuid(0x1531)   # Read+Write — duty 0-100%
CHAR_HAPTIC_STAT     = _uuid(0x1533)   # Read+Notify — 14B: [0]state(0-4) [1:5]single_shot LE [5:9]recurring LE [9:13]next_fire_epoch LE [13]flags
CHAR_HAPTIC_REMINDER = _uuid(0x1537)   # Read+Write — 8B: [0:4]single_shot LE [4:8]recurring LE

# Battery Service (SIG standard)
SVC_BATT          = "0000180f-0000-1000-8000-00805f9b34fb"
CHAR_BATT_LEVEL   = "00002a19-0000-1000-8000-00805f9b34fb"

# Device Information Service (SIG standard) — 5 chars exposed by firmware
SVC_DIS                 = "0000180a-0000-1000-8000-00805f9b34fb"
CHAR_DIS_SYSTEM_ID      = "00002a23-0000-1000-8000-00805f9b34fb"
CHAR_DIS_MODEL          = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_DIS_SERIAL         = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_DIS_FW_REV         = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_DIS_HW_REV         = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_DIS_MANUFACTURER   = "00002a29-0000-1000-8000-00805f9b34fb"

DEVICE_NAME       = "DUSQ-CHG"
AUTH_PIN_LEN      = 3
# Per-device PIN = low 24 bits of CRC32(UICR-backed Serial Number).
# See src/ble_svc.c auth_init().  Use compute_auth_pin() to derive it from
# the 16-byte serial read from DIS Serial Number (0x2A25).


# Flash block geometry (must match firmware src/flash.h)
BLOCK_SIZE          = 152      # full block: 32 B header + 30 × 4 B readings
HEADER_SIZE         = 32       # crc32 lives in the last 4 bytes of the header
READINGS_PER_BLOCK  = 30
BLOCK_MAGIC         = 0xDEADBEEF
# CRC over header bytes [0..27] only (firmware doesn't checksum the payload).
HEADER_CRC_RANGE    = HEADER_SIZE - 4  # = 28

# Journal geometry
JOURNAL_ENTRY_SIZE  = 32
JOURNAL_MAGIC       = 0xCAFEBABE
EVENT_TYPES = {
    0: "BOOT",          # data = (reset_cause << 24) | boot_count[23:0]
    1: "TIME_SYNC",     # data = epoch (the value just received)
    2: "FLASH_WRAP",    # data = wrapped block sequence
    3: "ERROR",         # data = error code
    4: "LOW_BATTERY",   # data = batt percent
}

# BOOT entry: high byte of data = reset cause (firmware RESET_CAUSE_* in app_config.h)
RESET_CAUSES = {
    0: "power-on/brown-out",
    1: "pin",
    2: "watchdog",
    3: "soft-reset",
    4: "lockup",
    5: "other",
    6: "app-fault",
}

# EVENT_ERROR (fault record) low-16 = NRF_FAULT_ID_* (app_error.h / nrf_sdm.h).
# Firmware packs the journal data as (info & 0xFFFF) << 16 | (id & 0xFFFF).
FAULT_IDS = {
    0x0001: "SoftDevice assert",
    0x1001: "app memacc / hardfault",
    0x4001: "SDK error (APP_ERROR_CHECK)",
    0x4002: "SDK assert (ASSERT)",
}

# Sensor error markers
TEMP_ERROR          = 127       # flash record (int8 degC)
TEMP_ERROR_BLE      = 0x7FFF    # BLE live char (int16 tenths-degC)
DB_ERROR            = 0xFF
LUX_ERROR           = 0xFFFF


# How long a download waits (s) after the last notification before declaring done.
DOWNLOAD_IDLE_TIMEOUT = 5.0

# Manufacturer Specific Data layout (firmware: src/ble_svc.c build_manuf_data)
#   [0]      sys_state (bits 0-3) | flags (bits 4-7: USB / boost / lid / auth)
#   [1]      battery % (0..100, 0xFF if not yet read)
#   [2]      unsynced block count, uint8 (0..234)
#   [3]      journal entry count, uint8 (0..128)
#   [4-7]    last sync Unix epoch, uint32 LE (0 = never)
#   [8-11]   haptic next-buzz, uint32 LE — absolute Unix epoch if [0] bit3=1 (time-synced),
#            else coarse seconds remaining; 0xFFFFFFFF = none (coarse ~minute resolution)
#   [12-15]  FICR DEVICEID[0], uint32 LE
#   [16-19]  FICR DEVICEID[1], uint32 LE
ADV_MSD_COMPANY_ID = 0xFFFF
ADV_MSD_LEN        = 20
ADV_STATE_NAMES = [
    "INIT", "SLOW_ADV", "FAST_ADV", "CONNECTED",
    "AUTHENTICATED", "FLASH_TX", "ERROR",
]

