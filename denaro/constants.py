from fastecdsa import curve
from decimal import Decimal

# cryptography
ENDIAN = 'little'
CURVE = curve.P256

# coin info
SMALLEST = 1000000
MAX_SUPPLY = 30_062_005
BLOCK_TIME = 30 # seconds
BLOCKS_DIFF_CHANGE = Decimal(120)
START_DIFF = Decimal('3.0')
VERSION = 1
MAX_BLOCK_SIZE_HEX = 4096 * 1024  # 4MB in HEX format, 2MB in raw bytes

# settings
DEV_NODE = "https://"
